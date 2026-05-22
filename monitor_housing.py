import hashlib
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, List, Dict, Any
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "last_sent.json"
CONFIG_PATH = BASE_DIR / "config.yaml"


@dataclass
class Notice:
    agency: str
    title: str
    url: str
    source_url: str
    status: str = ""
    region: str = ""
    category: str = ""
    posted_at: str = ""
    apply_period: str = ""
    target: str = ""

    @property
    def uid(self) -> str:
        raw = f"{self.agency}|{self.title}|{self.url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("config.yaml 파일이 없습니다.")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"sent_ids": []}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def guess_category(title: str, include_keywords: Iterable[str]) -> str:
    for kw in include_keywords:
        if kw in title:
            return kw
    return "임대주택"


def matches_filter(notice: Notice, include_keywords: List[str], exclude_keywords: List[str]) -> bool:
    haystack = " ".join([
        notice.title,
        notice.category,
        notice.region,
        notice.status,
        notice.target,
        notice.apply_period,
    ])
    if exclude_keywords and any(kw in haystack for kw in exclude_keywords):
        return False
    if include_keywords and not any(kw in haystack for kw in include_keywords):
        return False
    return True


def fetch_html(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 housing-notice-monitor/1.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    res = requests.get(url, headers=headers, timeout=timeout)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def parse_generic_table(html: str, source_url: str, agency: str, include_keywords: List[str]) -> List[Notice]:
    soup = BeautifulSoup(html, "html.parser")
    notices: List[Notice] = []

    # 1) table 기반 공고 목록 파싱
    for row in soup.select("table tr"):
        cells = [normalize(c.get_text(" ")) for c in row.select("th, td")]
        if len(cells) < 2:
            continue
        link = row.find("a", href=True)
        title = normalize(link.get_text(" ") if link else max(cells, key=len))
        if not title or len(title) < 5:
            continue
        url = urljoin(source_url, link["href"]) if link else source_url
        joined = " | ".join(cells)
        notices.append(
            Notice(
                agency=agency,
                title=title,
                url=url,
                source_url=source_url,
                status="접수/공고 확인 필요",
                region=extract_region(joined),
                category=guess_category(title + joined, include_keywords),
                posted_at=extract_date(joined),
                apply_period=extract_period(joined),
                target=extract_target(joined),
            )
        )

    # 2) 리스트형 공고 파싱 보강
    for a in soup.find_all("a", href=True):
        title = normalize(a.get_text(" "))
        if len(title) < 8:
            continue
        if not any(kw in title for kw in include_keywords):
            continue
        notices.append(
            Notice(
                agency=agency,
                title=title,
                url=urljoin(source_url, a["href"]),
                source_url=source_url,
                status="공고 확인 필요",
                region=extract_region(title),
                category=guess_category(title, include_keywords),
                posted_at=extract_date(title),
                apply_period=extract_period(title),
                target=extract_target(title),
            )
        )

    return dedupe_notices(notices)


def extract_date(text: str) -> str:
    patterns = [
        r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}",
        r"\d{4}\.\d{2}\.\d{2}",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0)
    return ""


def extract_period(text: str) -> str:
    m = re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}\s*[~∼-]\s*20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", text)
    return m.group(0) if m else "공고문 확인 필요"


def extract_region(text: str) -> str:
    regions = ["서울", "경기", "인천", "수원", "성남", "고양", "부천", "안양", "용인", "화성", "남양주", "의정부", "시흥"]
    found = [r for r in regions if r in text]
    return ", ".join(found[:3]) if found else "공고문 확인 필요"


def extract_target(text: str) -> str:
    targets = ["수급자", "주거급여", "차상위", "청년", "신혼", "한부모", "고령자", "장애인", "주거취약"]
    found = [t for t in targets if t in text]
    return ", ".join(found) if found else "공고문 확인 필요"


def dedupe_notices(notices: List[Notice]) -> List[Notice]:
    seen = set()
    result = []
    for n in notices:
        key = n.uid
        if key in seen:
            continue
        seen.add(key)
        result.append(n)
    return result


def collect_notices(config: Dict[str, Any]) -> List[Notice]:
    include_keywords = config.get("include_keywords", [])
    exclude_keywords = config.get("exclude_keywords", [])
    all_notices: List[Notice] = []

    for source in config.get("sources", []):
        agency = source.get("agency", "UNKNOWN")
        url = source.get("url")
        if not url:
            continue
        try:
            html = fetch_html(url)
            notices = parse_generic_table(html, url, agency, include_keywords)
            filtered = [n for n in notices if matches_filter(n, include_keywords, exclude_keywords)]
            all_notices.extend(filtered)
        except Exception as exc:
            all_notices.append(
                Notice(
                    agency=agency,
                    title=f"[오류] {agency} 공고 조회 실패: {exc}",
                    url=url,
                    source_url=url,
                    status="조회오류",
                    category="시스템",
                )
            )

    return dedupe_notices(all_notices)


def build_email_html(notices: List[Notice]) -> str:
    checked_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    items = []
    for idx, n in enumerate(notices, 1):
        items.append(
            f"""
            <tr>
              <td style='padding:8px;border:1px solid #ddd;'>{idx}</td>
              <td style='padding:8px;border:1px solid #ddd;'>{n.agency}</td>
              <td style='padding:8px;border:1px solid #ddd;'><a href='{n.url}'>{n.title}</a></td>
              <td style='padding:8px;border:1px solid #ddd;'>{n.category}</td>
              <td style='padding:8px;border:1px solid #ddd;'>{n.region}</td>
              <td style='padding:8px;border:1px solid #ddd;'>{n.apply_period}</td>
              <td style='padding:8px;border:1px solid #ddd;'>{n.target}</td>
              <td style='padding:8px;border:1px solid #ddd;'>{n.status}</td>
            </tr>
            """
        )

    return f"""
    <html><body>
    <h2>LH/SH 임대주택 신규 공고 알림</h2>
    <p>확인일시: {checked_at}</p>
    <p>신규 또는 미발송 공고 {len(notices)}건이 확인되었습니다.</p>
    <table style='border-collapse:collapse;width:100%;font-size:14px;'>
      <thead>
        <tr style='background:#f3f6fb;'>
          <th style='padding:8px;border:1px solid #ddd;'>No</th>
          <th style='padding:8px;border:1px solid #ddd;'>기관</th>
          <th style='padding:8px;border:1px solid #ddd;'>공고명</th>
          <th style='padding:8px;border:1px solid #ddd;'>유형</th>
          <th style='padding:8px;border:1px solid #ddd;'>지역</th>
          <th style='padding:8px;border:1px solid #ddd;'>접수기간</th>
          <th style='padding:8px;border:1px solid #ddd;'>신청대상</th>
          <th style='padding:8px;border:1px solid #ddd;'>상태</th>
        </tr>
      </thead>
      <tbody>{''.join(items)}</tbody>
    </table>
    <p style='margin-top:16px;color:#555;'>※ 자동 추출값은 공고문 원문 기준으로 최종 확인해야 합니다.</p>
    </body></html>
    """


def send_email(subject: str, html_body: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    mail_to = os.getenv("MAIL_TO")

    missing = [k for k, v in {
        "SMTP_USER": smtp_user,
        "SMTP_PASSWORD": smtp_password,
        "MAIL_TO": mail_to,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"필수 환경변수 누락: {', '.join(missing)}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=context)
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [mail_to], msg.as_string())


def main() -> None:
    config = load_config()
    state = load_state()
    sent_ids = set(state.get("sent_ids", []))

    notices = collect_notices(config)
    new_notices = [n for n in notices if n.uid not in sent_ids and n.status != "조회오류"]
    errors = [n for n in notices if n.status == "조회오류"]

    if not new_notices and not errors:
        print("신규 공고 없음. 메일 발송 생략.")
        return

    mail_notices = errors + new_notices
    subject = f"[LH/SH 임대주택 공고] 신규 공고 {len(new_notices)}건 확인"
    if errors:
        subject += f" / 조회오류 {len(errors)}건"

    send_email(subject, build_email_html(mail_notices))

    for n in new_notices:
        sent_ids.add(n.uid)
    state["sent_ids"] = sorted(sent_ids)
    state["last_checked_at"] = datetime.now(KST).isoformat()
    state["last_sent_count"] = len(new_notices)
    save_state(state)
    print(f"메일 발송 완료: 신규 {len(new_notices)}건, 오류 {len(errors)}건")


if __name__ == "__main__":
    main()
