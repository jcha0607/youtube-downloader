"""릴리스 설명(마크다운)을 만들어 표준출력으로 내보낸다.

환경변수
  VERSION    이번 앱 버전 (예: 1.0.43)
  PREV_TAG   직전 릴리스 태그 (예: v1.0.42), 없으면 빈 값
  YTDLP_NEW  이번에 번들한 yt-dlp 버전
  YTDLP_OLD  직전 릴리스에 번들됐던 yt-dlp 버전 (모르면 빈 값)
  REF        변경 내용을 읽을 커밋 (기본 HEAD)

'이번 업데이트'는 직전 릴리스 이후의 커밋 메시지(제목 + 본문의 항목)와 yt-dlp 버전 변화로 만든다.
"""
import os
import re
import subprocess

VERSION = os.environ.get("VERSION", "")
PREV_TAG = os.environ.get("PREV_TAG", "")
YTDLP_NEW = os.environ.get("YTDLP_NEW", "")
YTDLP_OLD = os.environ.get("YTDLP_OLD", "")
REF = os.environ.get("REF", "HEAD")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def commit_items():
    """직전 릴리스 이후 커밋을 [(제목, [세부 항목...]), ...] 로 반환 (오래된 것부터)"""
    if PREV_TAG and git("rev-parse", "-q", "--verify", f"refs/tags/{PREV_TAG}").strip():
        rng = [f"{PREV_TAG}..{REF}"]
    else:
        rng = ["-n", "5", REF]
    raw = git("log", "--no-merges", "--reverse", "--format=%s%x1f%b%x1e", *rng)
    items = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        subject, _, body = chunk.partition("\x1f")
        subs = []
        for line in body.splitlines():
            line = line.strip()
            if not line or re.match(r"(?i)^co-authored-by:", line):
                continue
            subs.append(re.sub(r"^[-*]\s+", "", line))
        items.append((subject.strip(), subs))
    return items


def norm(v):
    try:
        return ".".join(str(int(x)) for x in v.split("."))
    except ValueError:
        return v


def main():
    lines = []
    for subject, subs in commit_items():
        lines.append(f"- {subject}")
        lines += [f"  - {s}" for s in subs]

    if YTDLP_NEW and YTDLP_OLD and norm(YTDLP_NEW) != norm(YTDLP_OLD):
        lines.append(f"- yt-dlp(유튜브 다운로드 엔진)를 {YTDLP_OLD} → {YTDLP_NEW} 로 업데이트 "
                     "(유튜브 변경에 대응)")
    if not lines:
        lines.append("- 코드 변경 없음 (자동 재빌드)")

    prev = f" (이전 버전: {PREV_TAG})" if PREV_TAG else ""
    print("## 이번 업데이트")
    print("\n".join(lines))
    print(f"""
## 다운로드
- **Windows**: `YoutubeDownloader.exe`
- **Mac (Apple Silicon)**: `YoutubeDownloader.dmg` 를 열어 앱을 Applications 폴더로 끌어다 놓으세요.

### Mac 처음 실행 시
개발자 인증서로 서명된 앱이 아니라서 처음 한 번은 "열 수 없음" 경고가 뜹니다.
앱을 **우클릭 → 열기**를 하거나, 시스템 설정 → 개인정보 보호 및 보안에서 **그래도 열기**를 누르세요.
이후 업데이트는 앱 안의 "지금 업데이트" 버튼으로 자동 교체되어 이 과정이 필요 없습니다.

## 버전 정보
- 앱 버전: v{VERSION}{prev}
- yt-dlp: {YTDLP_NEW}""")


if __name__ == "__main__":
    main()
