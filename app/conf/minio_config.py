# 导入核心依赖：数据类、环境变量读取、路径处理
from dataclasses import dataclass
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

# 提前加载.env配置文件（确保os.getenv能获取到MinIO相关配置）
load_dotenv()


# 定义MinIO对象存储服务配置（与LLMConfig风格一致，字段对应.env配置项）
@dataclass
class MinIOConfig:
    endpoint: str    # 后端连接地址（容器内可用 minio:9000）
    public_endpoint: str  # 浏览器访问地址（容器映射到宿主机时用 localhost:9002 等）
    access_key: str  # MinIO访问密钥（对应MINIO_ACCESS_KEY）
    secret_key: str  # MinIO秘钥（对应MINIO_SECRET_KEY）
    bucket_name: str # MinIO默认存储桶名（知识库文件专用）
    minio_img_dir: str #Minio存储图片的文件夹
    minio_secure: bool # 是否使用ssl加密 http 还是 https


# 实例化MinIO配置对象，自动从.env读取配置并绑定
minio_config = MinIOConfig(
    endpoint=os.getenv("MINIO_ENDPOINT"),
    public_endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT") or os.getenv("MINIO_ENDPOINT"),
    access_key=os.getenv("MINIO_ACCESS_KEY"),
    secret_key=os.getenv("MINIO_SECRET_KEY"),
    bucket_name=os.getenv("MINIO_BUCKET_NAME"),
    # 默认与 .env.example 一致：CI / 未配 .env 时也不能是 None（模块导入期会用到）
    minio_img_dir=os.getenv("MINIO_IMG_DIR", "/upload-images"),
    minio_secure=os.getenv("MINIO_SECURE") == "True"
)


def _public_base() -> str:
    """浏览器访问 MinIO 的地址前缀。

    支持两种配置：
    - 绝对地址（http://host:9002）：浏览器直连 MinIO；
    - 相对路径（/rag-images 或 /）：图片与当前站点同源，由 Nginx / Vite
      反代到 MinIO —— 部署地址变化不会影响图片 URL。
    """
    endpoint = (minio_config.public_endpoint or "").strip()
    if endpoint.startswith("/"):
        return endpoint.rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"
    return endpoint.rstrip("/")


def public_image_url(object_path: str) -> str:
    """对象路径（如 upload-images/hak180/x.jpg）→ 浏览器可访问的图片 URL。

    导入时用它生成图片地址；相对前缀模式下得到 /rag-images/<bucket>/...
    """
    path = f"/{minio_config.bucket_name}/{object_path.lstrip('/')}"
    return f"{_public_base()}{path}"


def normalize_image_url(url: str) -> str:
    """把图片 URL 改写成当前配置的访问地址。

    图片 URL 在知识库导入时按当时的 MINIO_PUBLIC_ENDPOINT 生成，会随文档
    固化进向量库；部署地址（DHCP 换 IP、换域名）变化后旧主机访问不到，
    但 MinIO 对象路径不变。输出给前端前统一改写：
    - 相对前缀（/rag-images）：得到同源相对路径；
    - 绝对地址：只替换主机名。
    不是本系统 MinIO 的 URL（外链）原样返回。
    """
    if not url or not minio_config.bucket_name:
        return url
    if "://" in url:
        path = urlsplit(url).path
    elif url.startswith("/"):
        path = url
    else:
        return url
    if not path.startswith(f"/{minio_config.bucket_name}"):
        return url
    return f"{_public_base()}{path}"