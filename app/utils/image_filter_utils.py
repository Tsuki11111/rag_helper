"""
图片价值过滤：识别「操作步骤序号 / 装饰性图标」这类无意义图片

来由（2026-10-20 用户反馈）：HAK 180 说明书里有很多蓝色圆形数字徽标（1/2/3…），
视觉模型把它们描述成「蓝色圆形图标内含白色数字“3”，表示操作步骤序号」。
这些图没有信息量，却会进入切片、图片目录，甚至被答案引用成图卡。

判定策略是**宁漏勿错**，只拦同时满足三类的描述：

1. 徽标形态：蓝色圆形 / 蓝色背景 / 圆形图标 / 圆形标识；
2. 数字特征：数字 / 编号；
3. 步骤语义：步骤 / 序号 / 章节 / 「第 N 页(步/章)」。

单独的电源图标、操作面板示意图、部件分解图不会命中 —— 它们没有
「圆形 + 数字 + 步骤」三件套。命中后的处理：

- 导入侧（`node_md_img`）：不上传 MinIO，并从 Markdown 中删除引用；
- 查询侧（`node_answer_output._build_context`）：剥离同类图片链接，
  旧切片无需重导即可生效。
"""
import os
import re
import struct

_WS_RE = re.compile(r"\s+")

# 最长边不超过该值、且文件体积不超过 MAX_DECORATION_BYTES 的图片，
# 视为装饰图 / 步骤徽标 / 小图标。
# HAK 180 实测：步骤徽标 46~62px / 1.3~1.7KB，小图标 87~93px / 1.5~1.7KB，
# 真实插图 273~1453px / 10~170KB。双条件是为了避免误删「小但信息量大」的图
# （例如 100×100 但体积 20KB 的线框图）。
MIN_FIGURE_MAX_SIDE = 120
MAX_DECORATION_BYTES = 8 * 1024


def image_dimensions(path: str) -> tuple | None:
    """
    读取图片像素尺寸（不依赖 Pillow）

    支持 JPEG / PNG / GIF / BMP；WebP 等未支持格式返回 None（保守放行）。
    图片尺寸是**确定性**信号，比让视觉模型描述后再用正则猜要可靠。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    # PNG：签名 8 + 长度 4 + "IHDR" 4，随后是 width/height 各 4 字节
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)

    # GIF：Logical Screen Descriptor 的 width/height（小端）
    if len(data) >= 10 and data[:3] == b"GIF":
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)

    # BMP：BITMAPINFOHEADER 的 width/height（小端，可能为负）
    if len(data) >= 26 and data[:2] == b"BM":
        w, h = struct.unpack("<ii", data[18:26])
        return abs(int(w)), abs(int(h))

    # JPEG：扫描 SOF 标记；长度/精度之后是 height(2) + width(2)
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            if marker in (0xD8, 0xD9):
                i += 2
                continue
            if i + 4 > len(data):
                break
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len

    return None


def is_tiny_image(path: str, max_side: int = MIN_FIGURE_MAX_SIDE,
                  max_bytes: int = MAX_DECORATION_BYTES) -> bool:
    """
    小尺寸**且**小体积的图片视为装饰图

    只判尺寸会误删「小但信息量大」的线框图；加文件体积是第二道保险。
    尺寸或体积读不出来时保守放行（宁可多留，不可误删）。
    """
    dims = image_dimensions(path)
    if not dims or max(dims) > max_side:
        return False
    try:
        return os.path.getsize(path) <= max_bytes
    except OSError:
        return False


def is_meaningless_step_marker(caption: str) -> bool:
    """
    判断图片描述是否只是「步骤序号 / 装饰性数字徽标」（纯函数，离线可测）

    :param caption: 视觉模型生成的图片描述（Markdown alt 文本）
    :return: True = 无意义，不应进入知识库图片通道
    """
    text = _WS_RE.sub("", caption or "")
    if not text:
        return False

    if not any(m in text for m in ("蓝色圆形", "蓝色背景", "圆形图标", "圆形标识")):
        return False
    if not any(m in text for m in ("数字", "编号")):
        return False
    if any(m in text for m in ("步骤", "序号", "章节")):
        return True
    # 「第 7 页」「第 3 步」「第 2 章」这类
    return "第" in text and any(unit in text for unit in ("页", "步", "章"))


def _check_step_marker_filter() -> list:
    """
    离线自测：步骤序号图的判定（纯逻辑，零依赖）

    正例取自 HAK 180 的真实视觉描述；负例是容易被误伤的有用图片。
    规则一旦放宽到「圆形 + 数字」就会误删操作面板按钮图，必须钉住。
    """
    problems = []

    positives = [
        "蓝色圆形图标内有白色数字“3”，表示操作步骤序号，指示当前为第三步操作。",
        "蓝色圆形图标内含白色数字“1”，为说明书中的章节或步骤编号标识。",
        "蓝色圆形背景中央显示白色数字“7”，为说明书第7页或章节标识图。",
        "图示为编号“1”的蓝色圆形标识，对应步骤中需去除的固定胶带。",
        "图中蓝色圆形内有白色数字“5”，表示操作步骤序号，对应取出烫金膜盒的第5步操作。",
        "蓝色圆形背景中白色数字“5”，可能为说明书中的章节或步骤编号标识。",
    ]
    negatives = [
        "",
        "HAK 180设备外观示意图，展示其主体结构、控制面板及前后纸张输出/输入托盘。",
        "电源开关图标，表示打开或关闭设备电源功能。",
        "图示说明设备面板上三个指示灯：①节省烫金膜（Foil Save）、②盖锁定（Cover Lock）、③错误（Error）。",
        "图示为设备启动按钮，上方标有数字“7”，下方标注“（Start）（启动）”。",
        # 圆形按钮 + 数字，但没有步骤/序号/章节语义 —— 可能是真正有用的控制按钮
        "蓝色圆形按钮，标注数字3，表示温度设置按钮。",
    ]

    for c in positives:
        if not is_meaningless_step_marker(c):
            problems.append(f"步骤序号图漏判：{c!r}")
    for c in negatives:
        if is_meaningless_step_marker(c):
            problems.append(f"有用图片被误判：{c!r}")

    # 尺寸判定：合成最小 PNG / JPEG 头，验证像素解析与小图判定
    import os
    import tempfile

    png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
           + struct.pack(">II", 46, 45))
    jpeg = (b"\xff\xd8\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">HH", 45, 46)
            + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00")
    with tempfile.TemporaryDirectory() as d:
        tiny = os.path.join(d, "tiny.png")
        fig = os.path.join(d, "fig.png")
        with open(tiny, "wb") as f:
            f.write(png)
        with open(fig, "wb") as f:
            f.write(png[:16] + struct.pack(">II", 415, 520))
        if image_dimensions(tiny) != (46, 45):
            problems.append(f"PNG 尺寸解析不对：{image_dimensions(tiny)!r}")
        if not is_tiny_image(tiny):
            problems.append("46×45 的步骤徽标应判为小图")
        if is_tiny_image(fig):
            problems.append("415×520 的真实插图不应判为小图")
        jpg = os.path.join(d, "badge.jpg")
        with open(jpg, "wb") as f:
            f.write(jpeg)
        if image_dimensions(jpg) != (46, 45):
            problems.append(f"JPEG 尺寸解析不对：{image_dimensions(jpg)!r}")
        if not is_tiny_image(jpg):
            problems.append("46×45 的 JPEG 步骤徽标应判为小图")
        # 小尺寸但体积大：可能是线框图，不能只看尺寸
        heavy = os.path.join(d, "heavy.png")
        with open(heavy, "wb") as f:
            f.write(png[:16] + struct.pack(">II", 100, 100) + b"\0" * 9000)
        if is_tiny_image(heavy):
            problems.append("100×100 但 9KB 的图片不应判为装饰图")

    return problems
