import hashlib
import logging
import random
import time
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# ---------- constants ----------
SIGN_KEY = "tiebaclient!!!"
TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
LIKE_URL = "https://c.tieba.baidu.com/c/f/forum/like"
SIGN_URL = "https://c.tieba.baidu.com/c/c/forum/sign"
WEB_SIGN_URL = "https://tieba.baidu.com/sign/add"
MOBILE_SIGN_URL = "https://tieba.baidu.com/mo/q/sign"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/95.0.4638.69 Safari/537.36"
    ),
}

BASE_SIGN_DATA = {
    "_client_type": "2",
    "_client_version": "9.7.8.0",
    "_phone_imei": "000000000000000",
    "model": "MI+5",
    "net_type": "1",
}


# ---------- client ----------
class TiebaClient:
    """
    百度贴吧签到客户端
    """

    def __init__(self, bduss: str, stoken: Optional[str] = None) -> None:
        if not bduss:
            raise ValueError("BDUSS 不能为空")
        self.bduss = bduss
        self.stoken = stoken
        self._session: Optional[requests.Session] = None

    # -- session 惰性初始化 --
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(HEADERS)
            # 将认证信息注入 Cookie，否则服务端收不到认证信息
            cookies = {"BDUSS": self.bduss}
            if self.stoken:
                cookies["STOKEN"] = self.stoken
            cookies["BAIDUID"] = hashlib.md5(
                str(time.time_ns()).encode()
            ).hexdigest().upper()
            requests.utils.add_dict_to_cookiejar(
                self._session.cookies, cookies
            )
        return self._session

    # -- 签名算法 --
    @staticmethod
    def signature(data: dict) -> str:
        s = "".join(f"{k}={data[k]}" for k in sorted(data))
        return hashlib.md5((s + SIGN_KEY).encode()).hexdigest().upper()

    # -- 带指数退避的请求 --
    def _request(
        self,
        url: str,
        method: str = "get",
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        retry: int = 3,
    ) -> Optional[dict]:
        for i in range(retry):
            try:
                if method.lower() == "get":
                    resp = self.session.get(url, headers=headers, timeout=10)
                else:
                    resp = self.session.post(url, data=data, headers=headers, timeout=10)

                resp.raise_for_status()
                if not resp.text.strip():
                    raise ValueError("空响应")
                return resp.json()
            except Exception as e:
                if i == retry - 1:
                    logger.error(f"请求失败(已重试 {retry} 次): {e}")
                    return None
                wait = 1.5 * (2**i) + random.uniform(0, 1)
                logger.warning(f"请求异常，{wait:.1f}s 后重试 ({i+1}/{retry}): {e}")
                time.sleep(wait)
        return None

    # -- 获取 tbs --
    def get_tbs(self) -> Optional[str]:
        """获取 tbs，BDUSS 有效性由后续签到请求自然验证。"""
        result = self._request(TBS_URL)
        if result is None:
            logger.error("获取 tbs 失败")
            return None
        return result.get("tbs", "")

    # -- 获取关注的贴吧列表 --
    def get_favorites(self) -> list[dict]:
        forums: list[dict] = []
        page_no = 1

        while True:
            data = {
                "BDUSS": self.bduss,
                "_client_type": "2",
                "_client_id": "wappc_1534235498291_488",
                "_client_version": "9.7.8.0",
                "_phone_imei": "000000000000000",
                "from": "1008621y",
                "page_no": str(page_no),
                "page_size": "200",
                "model": "MI+5",
                "net_type": "1",
                "timestamp": str(int(time.time())),
                "vcode_tag": "11",
            }
            data["sign"] = self.signature(data)

            result = self._request(LIKE_URL, "post", data)
            if result is None:
                logger.warning("获取贴吧列表失败，停止翻页")
                break

            if "forum_list" in result:
                for forum_type in ("non-gconforum", "gconforum"):
                    items = result["forum_list"].get(forum_type, [])
                    if isinstance(items, list):
                        forums.extend(items)
                    elif isinstance(items, dict):
                        forums.append(items)

            if result.get("has_more") != "1":
                break

            page_no += 1
            time.sleep(random.uniform(1, 2))

        logger.info(f"共获取到 {len(forums)} 个关注的贴吧")
        return forums

    # -- 单个贴吧签到 --
    def sign_forum(self, fid: str, name: str, tbs: str) -> dict:
        """
        返回 {
            "status": "success" | "exist" | "shield" | "error",
            "rank": Optional[int],   -- 签到排名 (仅 success)
            "message": str,
        }
        """
        data = {**BASE_SIGN_DATA}
        data.update(
            {
                "BDUSS": self.bduss,
                "fid": fid,
                "kw": name,
                "tbs": tbs,
                "timestamp": str(int(time.time())),
            }
        )
        data["sign"] = self.signature(data)

        result = self._request(SIGN_URL, "post", data)
        if result is None:
            return {"status": "error", "rank": None, "message": "网络请求失败"}

        error_code = result.get("error_code", "")
        error_msg = result.get("error_msg", "")

        if error_code == "0":
            rank = None
            if "user_info" in result:
                rank = result["user_info"].get("user_sign_rank")
                rank = int(rank) if rank else None
            return {"status": "success", "rank": rank, "message": "签到成功"}
        elif error_code == "160002":
            return {"status": "exist", "rank": None, "message": error_msg or "今日已签到"}
        elif error_code == "340006":
            return {"status": "shield", "rank": None, "message": "贴吧已被屏蔽"}
        else:
            return {"status": "error", "rank": None, "message": error_msg or "未知错误"}

    # -- Web/WAP 端签到兜底 --
    def sign_forum_web(self, fid: str, name: str, tbs: str) -> dict:
        """客户端接口异常时，优先使用 WAP 签到接口，再回退到桌面端接口。"""
        if not self.stoken:
            logger.warning(f"〖{name}〗未配置 STOKEN，跳过 Web/WAP 兜底")

        # WAP 签到接口比 /sign/add 对部分特殊贴吧兼容性更好；
        # 关键是同时提供 fid、kw、tbs 和 is_like=1。
        mobile_params = (
            f"tbs={quote(str(tbs))}&"
            f"kw={quote(str(name))}&"
            "is_like=1&"
            f"fid={quote(str(fid))}"
        )
        mobile_url = f"{MOBILE_SIGN_URL}?{mobile_params}"
        mobile_headers = {
            "Referer": f"https://tieba.baidu.com/f?kw={quote(name)}",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; K) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
        }

        result = self._request(mobile_url, "get", headers=mobile_headers)
        if result is not None:
            no = result.get("no")
            error = result.get("error", "")
            if no in (0, "0"):
                return {"status": "success", "rank": None, "message": "签到成功（WAP 端）"}
            if no in (1101, "1101", 160002, "160002"):
                return {"status": "exist", "rank": None, "message": error or "今日已签到（WAP 端）"}
            logger.warning(
                f"〖{name}〗WAP 签到返回 no={no!r}, error={error!r}，继续尝试桌面端签到"
            )

        # WAP 失败时保留原来的 Web 接口作为最后兜底。
        if self.stoken:
            data = {
                "ie": "utf-8",
                "kw": name,
                "tbs": tbs,
            }
            headers = {
                "Referer": f"https://tieba.baidu.com/f?kw={quote(name)}&fr=home",
                "X-Requested-With": "XMLHttpRequest",
            }
            result = self._request(WEB_SIGN_URL, "post", data, headers=headers)
            if result is not None:
                no = result.get("no")
                try:
                    no_int = int(no) if no is not None else -1
                except (TypeError, ValueError):
                    no_int = -1
                error = result.get("error", "")
                if no_int == 0:
                    return {"status": "success", "rank": None, "message": "签到成功（Web 端）"}
                if no_int == 1101:
                    return {"status": "exist", "rank": None, "message": error or "今日已签到（Web 端）"}
                return {
                    "status": "error",
                    "rank": None,
                    "message": error or f"Web/WAP 签到失败，错误码 {no_int}",
                }

        return {
            "status": "error",
            "rank": None,
            "message": "Web/WAP 签到请求失败",
        }
