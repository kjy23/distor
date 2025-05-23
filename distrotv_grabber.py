import requests
import time
from datetime import datetime
from threading import Lock
# 确保已安装 py-xmltv 包，因为 xmltv.models 由它提供
from xmltv.models import xmltv
from xsdata.formats.dataclass.serializers import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

class Client:
    """
    负责与 DistroTV API 交互，获取频道和节目单信息。
    """
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 9; AFTT Build/STT9.221129.002) GTV/AFTT DistroTV/2.0.9'
        })
        self.lock = Lock()
        self.feed = None
        self.feedTime = 0

    def load_feed(self):
        """
        从 DistroTV API 加载频道和节目概览信息。
        包含缓存机制，12小时内不重复加载。
        """
        with self.lock:
            # 缓存机制：如果数据存在且未过期（12小时内），则直接返回
            if self.feed is not None and time.time() - self.feedTime < 3600 * 12:
                print("使用缓存的频道数据...")
                return

            print("正在从 API 加载频道数据...")
            try:
                # 设置请求超时
                data = self.session.get("https://tv.jsrdn.com/tv_v5/getfeed.php", timeout=10).json()
                self.feed = {
                    "topics": [t for t in data["topics"] if t["type"] == "live"],
                    "shows": {k: v for k, v in data["shows"].items() if v["type"] == "live"},
                }
                self.feedTime = time.time()
                print("频道数据加载成功。")
            except requests.exceptions.RequestException as e:
                print(f"加载频道数据失败: {e}")
                self.feed = None # 清空feed，确保下次会重试
                raise # 重新抛出异常，让调用者知道加载失败

    def channels(self):
        """
        获取整理后的直播频道列表。
        每个频道包含 id, guideId, logo, genre, description, name, url 等信息。
        """
        try:
            self.load_feed() # 确保数据已加载
        except Exception as e:
            # load_feed 可能会抛出异常，这里捕获并返回错误信息
            return [], f"无法加载频道数据: {e}"

        if self.feed is None:
            return [], "频道数据为空，无法获取频道列表。"

        stations = []
        for ch in self.feed["shows"].values():
            # 确保关键字段存在，避免KeyError
            if not all(k in ch for k in ["name", "img_logo", "description", "title"]):
                print(f"警告: 频道数据不完整，跳过: {ch.get('title', '未知频道')}")
                continue

            # 确保直播流URL路径存在
            if not (ch.get("seasons") and ch["seasons"] and 
                    ch["seasons"][0].get("episodes") and ch["seasons"][0]["episodes"] and 
                    ch["seasons"][0]["episodes"][0].get("content") and 
                    ch["seasons"][0]["episodes"][0]["content"].get("url")):
                print(f"警告: 频道 {ch.get('title', '未知')} 缺少直播流URL，跳过。")
                continue

            station_info = {
                "id": ch["name"], # 用于 channel-id 和 tvg-id
                "guideId": ch["name"], 
                "logo": ch["img_logo"], # 用于 tvg-logo
                "genre": ch.get("genre", ""), # 使用 .get() 提供默认值，以防字段不存在
                "keywords": ch.get("keywords", ""), # 同上
                "description": ch["description"].strip(), # 用于 tvg-description
                "name": ch["title"].strip(), # 用于显示名称和 tvg-name
                # 从嵌套结构中提取直播流 URL，并移除查询参数
                "url": ch["seasons"][0]["episodes"][0]["content"]["url"].split('?', 1)[0]
            }
            stations.append(station_info)
        return stations, None

    def epg(self):
        """
        生成 XMLTV 格式的电子节目单 (EPG)。
        """
        try:
            self.load_feed()
        except Exception as e:
            print(f"无法加载频道数据以生成 EPG: {e}")
            return ""

        if self.feed is None:
            return "" # 返回空字符串或抛出异常

        epg = xmltv.Tv(
            source_info_name="distrotv",
            generator_info_name="vlc-bridge"
        )
        ids = {}
        for ch in self.feed["shows"].values():
            # 确保 ch["seasons"][0]["episodes"][0]["id"] 存在且可转换为字符串
            if ch.get("seasons") and ch["seasons"][0].get("episodes") and ch["seasons"][0]["episodes"][0].get("id"):
                ids[str(ch["seasons"][0]["episodes"][0]["id"])] = ch["name"]
                epg.channel.append(xmltv.Channel(
                    id=ch["name"],
                    display_name=[ch["title"].strip()]
                ))
            else:
                # print(f"警告: 频道 {ch.get('title', '未知')} 缺少 EPG ID 信息，将跳过其 EPG 查询。")
                pass # 避免过多警告信息，除非在调试

        if not ids:
            # print("没有有效的 EPG ID 可供查询。")
            return "" # 没有可查询的 EPG

        # print("正在查询 EPG 数据...")
        try:
            data = self.session.get("https://tv.jsrdn.com/epg/query.php?id=" + ",".join(ids.keys()), timeout=15).json()
            # print("EPG 数据查询成功。")
        except requests.exceptions.RequestException as e:
            print(f"查询 EPG 数据失败: {e}")
            return ""

        for id, name in ids.items():
            if (ch_epg := data["epg"].get(id)) is not None and (slots := ch_epg.get("slots")) is not None:
                for slot in slots:
                    try:
                        # 检查 start 和 end 字段是否存在
                        if "start" not in slot or "end" not in slot:
                            # print(f"警告: 频道 {name} 的 EPG 时段缺少 'start' 或 'end' 时间，跳过。")
                            continue

                        start_time = datetime.strptime(slot["start"], '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S') + " +0000"
                        stop_time = datetime.strptime(slot["end"], '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S') + " +0000"
                        epg.programme.append(xmltv.Programme(
                            channel=name,
                            title=slot.get("title", "").strip(), # 使用.get()确保安全访问
                            desc=(slot.get("description") or "").strip(),
                            icon=slot.get("img_thumbh", ""), # 使用.get()以防键不存在
                            start=start_time,
                            stop=stop_time,
                        ))
                    except (KeyError, ValueError) as e:
                        print(f"警告: 处理频道 {name} 的 EPG 时段 {slot.get('title', '未知')} 发生错误: {e}")
                        continue # 跳过当前有问题的时段

        serializer = XmlSerializer(config=SerializerConfig(
            pretty_print=True,
            encoding="UTF-8",
            xml_version="1.1",
            xml_declaration=False,
            no_namespace_schema_location=None
        ))
        return serializer.render(epg)

def generate_m3u(output_file="distrotv_channels.m3u", epg_url=None):
    """
    生成 M3U 播放列表文件，包含频道信息和直播流 URL。

    Args:
        output_file (str): 生成的 M3U 文件名。
        epg_url (str, optional): 可选的 EPG XMLTV 文件的 URL。如果提供，
                                  会在 M3U 头部添加 url-tvg 属性。
    """
    client = Client()
    stations, error = client.channels()

    if error:
        print(f"错误: {error}")
        return

    if not stations:
        print("没有获取到任何频道数据，M3U 文件将为空。")
        return

    print(f"正在生成 M3U 文件 '{output_file}'...")
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            # M3U 文件头部
            if epg_url:
                f.write(f"#EXTM3U url-tvg=\"{epg_url}\"\n")
            else:
                f.write("#EXTM3U\n")

            # 遍历频道信息并写入 M3U 条目
            for station in stations:
                # 简单处理 group-title，可以根据需要进行更复杂的分类
                group_title = "Others" # 默认分组
                if station["genre"]:
                    # 尝试取第一个逗号前的分类
                    group_title = station["genre"].split(',')[0].strip()
                elif station["keywords"]:
                    group_title = station["keywords"].split(',')[0].strip()

                extinf_line = (
                    f'#EXTINF:-1 '
                    f'channel-id="{station["id"]}" '
                    f'tvg-id="{station["id"]}" '
                    f'tvg-logo="{station["logo"]}" '
                    f'tvg-description="{station["description"]}" '
                    f'group-title="{group_title}",'
                    f'{station["name"]}\n'
                )
                f.write(extinf_line)
                f.write(f'{station["url"]}\n')

        print(f"M3U 播放列表已成功生成到 '{output_file}'。")
    except IOError as e:
        print(f"保存 M3U 文件失败: {e}")
    except Exception as e:
        print(f"生成 M3U 文件时发生未知错误: {e}")


# --- 主执行块 ---
if __name__ == "__main__":
    # 1. 定义 EPG XMLTV 文件的 URL
    # <<<<<<<<<<<<<<<< 请在这里更新您的 EPG 公共 URL！ >>>>>>>>>>>>>>>>>>
    # 这是 DistroTV EPG XML文件在您的GitHub仓库中的原始文件URL。
    # 格式为: https://raw.githubusercontent.com/您的用户名/您的仓库名/您的分支/distrotv_epg.xml
    # 示例 (根据您的仓库 'kjy23/distor' 和 'master' 分支):
    my_epg_xmltv_url = "https://raw.githubusercontent.com/kjy23/distor/master/distrotv_epg.xml" 
    # <<<<<<<<<<<<<<<< 确保将 'master' 替换为您实际使用的分支名，如果不是的话。 >>>>>>>>>>>>>>>>>>

    # 2. 调用生成 M3U 文件的函数
    print("\n--- 正在生成 M3U 播放列表 ---")
    generate_m3u(output_file="distrotv_channels.m3u", epg_url=my_epg_xmltv_url)

    # 3. 生成 XMLTV EPG 文件：此代码块确保 distrotv_epg.xml 被生成
    print("\n--- 尝试生成 XMLTV EPG 文件 (这可能需要一些时间) ---")
    client_for_epg_generation = Client()
    try:
        epg_xml_content = client_for_epg_generation.epg()
        if epg_xml_content:
            with open("distrotv_epg.xml", "w", encoding="utf-8") as f:
                f.write(epg_xml_content)
            print("XMLTV EPG 文件已成功生成到 'distrotv_epg.xml'。")
        else:
            print("未能生成 XMLTV EPG 文件，可能没有 EPG 数据或发生错误。")
    except Exception as e:
        print(f"生成 EPG 文件失败: {e}")

    print("\n--- 脚本执行完毕 ---")