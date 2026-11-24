"""달스오토 클라우드 발행기 — GitHub Actions가 15분마다 실행(PC가 꺼져 있어도 발행).

queue.json  : 집 PC의 달스오토가 미리 만든 예약 글(본문·타래·공개 사진 주소·예약 시각·링크 규칙)
state.json  : 이 스크립트가 기록하는 결과(발행 id·주소·조회수·링크 게시 여부) → 집 PC가 받아서 DB에 반영

규칙은 집 PC 달스오토와 같다:
- 예약 시각이 지난 글만, 한 번에 1편, 앞 발행과 최소 간격 유지, 하루 상한
- 발행 실패는 20분 뒤 1번 재시도
- 조회 1,000↑(48시간 안) 글에만 링크 타래, 단 발행 30분~1시간 30분 측정값이 100 미만이면 링크 제외(30분 컷)
- 72시간 안 글은 30분마다, 14일 안 글은 12시간마다 조회수 측정
토큰은 GitHub Secrets(DALSAUTO_THREADS_TOKEN)에서만 읽는다. 표준 라이브러리만 쓴다."""
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GRAPH = "https://graph.threads.net/v1.0"
ROOT = Path(__file__).resolve().parent
QUEUE, STATE = ROOT / "queue.json", ROOT / "state.json"
DAILY_CAP, MIN_GAP_MIN, RETRY_MIN = 8, 50, 20
UTC = timezone.utc


class ApiError(Exception):
    pass


def call(method, path, **params):
    tok = os.environ["DALSAUTO_THREADS_TOKEN"]
    data = urllib.parse.urlencode(params)
    url = f"{GRAPH}/{path}" + ("" if method == "POST" else "?" + data)
    req = urllib.request.Request(url, data=data.encode() if method == "POST" else None, method=method,
                                 headers={"Authorization": f"Bearer {tok}"})  # 토큰은 주소가 아니라 헤더로
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise ApiError(f"API {e.code}: {msg[:200]}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"네트워크 오류: {e.reason}") from None


# ---------- 저장소가 공개라서 queue.json·state.json은 암호화해 둔다(예약 글·조회수 노출 방지) ----------
MAGIC = "DALS1:"


def _fernet():
    tok = os.environ.get("DALSAUTO_THREADS_TOKEN")
    if not tok:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dalsauto-cloud-v1:" + tok.encode()).digest()))


def load_json(path, default):
    """암호화(DALS1:…) 또는 평문 JSON을 읽는다. 복호화 실패(토큰을 갈아 끼운 직후)나 없는 파일은 default."""
    if not Path(path).exists():
        return default
    txt = Path(path).read_text(encoding="utf-8")
    if txt.startswith(MAGIC):
        f = _fernet()
        try:
            txt = f.decrypt(txt[len(MAGIC):].encode()).decode("utf-8")
        except Exception:
            print(f"{Path(path).name} 복호화 실패 — 새로 시작(집 PC 큐의 발행 기록으로 복원됨)")
            return default
    return json.loads(txt)


def save_json(path, obj):
    txt = json.dumps(obj, ensure_ascii=False)
    f = _fernet()
    Path(path).write_text(MAGIC + f.encrypt(txt.encode()).decode() if f else json.dumps(obj, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def wait_ready(cid):
    for _ in range(40):
        d = call("GET", cid, fields="status,error_message")
        if d.get("status") == "FINISHED":
            return
        if d.get("status") in ("ERROR", "EXPIRED"):
            raise ApiError(f"컨테이너 {d.get('status')}: {d.get('error_message', '')}")
        time.sleep(2)
    raise ApiError("컨테이너 준비 시간 초과")


def create(uid, **p):
    return call("POST", f"{uid}/threads", **p)["id"]


def publish_chain(uid, body, images, threads):
    imgs = [u for u in images if str(u).startswith("https://")][:20]
    if not imgs:
        cid = create(uid, media_type="TEXT", text=body)
    elif len(imgs) == 1:
        cid = create(uid, media_type="IMAGE", text=body, image_url=imgs[0])
    else:
        kids = [create(uid, media_type="IMAGE", image_url=u, is_carousel_item="true") for u in imgs]
        for k in kids:
            wait_ready(k)
        cid = create(uid, media_type="CAROUSEL", text=body, children=",".join(kids))
    wait_ready(cid)
    root = call("POST", f"{uid}/threads_publish", creation_id=cid)["id"]
    prev, replies = root, []
    for t in threads:
        rc = create(uid, media_type="TEXT", text=t, reply_to_id=prev)
        wait_ready(rc)
        prev = call("POST", f"{uid}/threads_publish", creation_id=rc)["id"]
        replies.append(prev)
    link = call("GET", root, fields="permalink").get("permalink")
    return root, link, replies


def reply(uid, text, to):
    rc = create(uid, media_type="TEXT", text=text, reply_to_id=to)
    wait_ready(rc)
    return call("POST", f"{uid}/threads_publish", creation_id=rc)["id"]


def insights(mid):
    d = call("GET", f"{mid}/insights", metric="views,likes,replies,reposts,quotes,shares")
    return {x["name"]: (x.get("values") or [{}])[0].get("value") for x in d.get("data", [])}


def iso(t):
    return t.isoformat(timespec="seconds")


def hours(a, b):
    return (b - datetime.fromisoformat(a)).total_seconds() / 3600


def find_recent_same(uid, body, now):
    """같은 본문이 최근 12시간 안에 이미 올라갔으면 그 글을 돌려준다(실행이 겹치거나 상태 저장이 유실돼도 중복 발행 방지).
    확인 자체가 실패하면 None — 평소대로 발행한다."""
    try:
        data = call("GET", f"{uid}/threads", fields="id,permalink,text,timestamp", limit=10).get("data", [])
    except ApiError:
        return None
    want = " ".join(body.split())
    for x in data:
        try:
            at = datetime.fromisoformat(x["timestamp"].replace("+0000", "+00:00"))
        except (KeyError, ValueError):
            continue
        if " ".join((x.get("text") or "").split()) == want and now - at < timedelta(hours=12):
            return {**x, "at": at}
    return None


def run(now=None):
    now = now or datetime.now(UTC)
    queue = load_json(QUEUE, {"posts": [], "rules": {}})
    state = load_json(STATE, {"posts": {}})
    rules = queue.get("rules", {})
    for p in queue["posts"]:  # 집 PC가 이미 발행한 글은 발행됨으로 기록(중복 발행 방지)
        if p.get("published") and str(p["id"]) not in state["posts"]:
            pb = p["published"]
            state["posts"][str(p["id"])] = {"status": "published", **{k: v for k, v in pb.items() if v}}
    th, window = int(rules.get("viral_threshold", 1000)), int(rules.get("viral_window_hours", 48))
    log = []
    uid = call("GET", "me", fields="id")["id"]

    # 1) 발행: 예약 시각 지난 글 1편(간격·하루 상한 지킴)
    pub_times = [datetime.fromisoformat(s["published_at"]) for s in state["posts"].values() if s.get("published_at")]
    today = [t for t in pub_times if t >= now - timedelta(hours=24)]
    last = max(pub_times) if pub_times else None
    due = sorted((p for p in queue["posts"]
                  if state["posts"].get(str(p["id"]), {}).get("status") not in ("published", "failed")
                  and p["scheduled_at"] <= iso(now)
                  and state["posts"].get(str(p["id"]), {}).get("retry_at", "") <= iso(now)),
                 key=lambda p: p["scheduled_at"])
    if due and len(today) < DAILY_CAP and (not last or now - last >= timedelta(minutes=MIN_GAP_MIN)):
        p = due[0]
        st = state["posts"].setdefault(str(p["id"]), {})
        dup = find_recent_same(uid, p["body"], now)
        if dup:  # 이미 올라간 글: 다시 올리지 않고 기록만 한다
            st.update(status="published", published_at=iso(dup["at"]), threads_id=dup["id"],
                      permalink=dup.get("permalink"), reply_ids=[])
            log.append(f"이미 올라간 글 감지 #{p['id']} {dup.get('permalink')} — 기록만")
        else:
            try:
                threads = list(p.get("threads") or [])
                if p.get("link_mode") == "immediate" and p.get("link_thread"):
                    threads.append(p["link_thread"])
                root, link, replies = publish_chain(uid, p["body"], p.get("images") or [], threads)
                st.update(status="published", published_at=iso(now), threads_id=root, permalink=link, reply_ids=replies)
                log.append(f"발행 #{p['id']} {link}")
            except ApiError as e:
                if st.get("tries", 0) >= 1:
                    st.update(status="failed", error=f"재시도 후 실패: {e}")
                else:
                    st.update(tries=1, retry_at=iso(now + timedelta(minutes=RETRY_MIN)), error=f"재시도 예정: {e}")
                log.append(f"실패 #{p['id']}: {e}")

    # 2) 조회수 측정 + 터진 글 링크 타래
    qmap = {str(p["id"]): p for p in queue["posts"]}
    for pid, st in state["posts"].items():
        if st.get("status") != "published":
            continue
        if pid not in qmap:
            continue  # 큐에서 빠진 글(다른 계정 시험 글 등)은 측정하지 않는다
        age = hours(st["published_at"], now)
        if age > 24 * 14:
            continue
        every = 0.5 if age <= 72 else 12
        if st.get("metrics_at") and hours(st["metrics_at"], now) < every:
            continue
        try:
            m = insights(st["threads_id"])
        except ApiError as e:
            log.append(f"측정 실패 #{pid}: {e}")
            continue
        st.update(metrics=m, views=m.get("views") or 0, metrics_at=iso(now))
        st.setdefault("history", []).append([iso(now), st["views"]])
        st["history"] = st["history"][-60:]
        if 0.5 <= age <= 1.5 and "early_views" not in st:
            st["early_views"] = st["views"]
        p = qmap.get(pid, {})
        if (p.get("link_mode") == "on_viral" and p.get("link_thread") and not st.get("link_posted_at")
                and age <= window and st["views"] >= th and st.get("early_views", th) >= 100):
            try:
                target = (st.get("reply_ids") or [st["threads_id"]])[-1]
                st["link_reply_id"] = reply(uid, p["link_thread"], target)
                st["link_posted_at"] = iso(now)
                log.append(f"링크 타래 #{pid} (조회 {st['views']})")
            except ApiError as e:
                log.append(f"링크 실패 #{pid}: {e}")

    state["updated_at"] = iso(now)
    state.setdefault("log", []).extend(f"{iso(now)} {x}" for x in log)
    state["log"] = state["log"][-200:]
    save_json(STATE, state)
    print("\n".join(log) or "할 일 없음")


WAIT_WINDOW_MIN = 20  # 한 번 실행에서 이 시간 안에 예약 시각이 오는 글은 기다렸다가 정확히 올린다


def seconds_until_next_due(now=None):
    """WAIT_WINDOW_MIN분 안에 올릴 글이 있으면 기다릴 초, 없으면 None. 발행 간격(MIN_GAP_MIN)도 지킨다."""
    now = now or datetime.now(UTC)
    queue = load_json(QUEUE, {"posts": []})
    state = load_json(STATE, {"posts": {}})
    pubs = [datetime.fromisoformat(s["published_at"]) for s in state["posts"].values() if s.get("published_at")]
    earliest_gap = (max(pubs) + timedelta(minutes=MIN_GAP_MIN)) if pubs else now
    best = None
    for p in queue["posts"]:
        st = state["posts"].get(str(p["id"]), {})
        if p.get("published") or st.get("status") in ("published", "failed"):
            continue
        at = max(datetime.fromisoformat(p["scheduled_at"]),
                 datetime.fromisoformat(st["retry_at"]) if st.get("retry_at") else now, earliest_gap)
        if best is None or at < best:
            best = at
    if best is None or best - now > timedelta(minutes=WAIT_WINDOW_MIN):
        return None
    return max(0.0, (best - now).total_seconds()) + 2


if __name__ == "__main__":
    if not os.environ.get("DALSAUTO_THREADS_TOKEN"):
        sys.exit("DALSAUTO_THREADS_TOKEN 시크릿이 없음 — 저장소 Settings > Secrets 에 등록 필요")
    run()
    for _ in range(3):  # 곧 올릴 글이 있으면 기다렸다가 정확한 시각에(공개 저장소라 실행 시간 무제한)
        wait = seconds_until_next_due()
        if wait is None:
            break
        print(f"{int(wait)}초 뒤 다음 글 발행 대기")
        time.sleep(wait)
        run()
