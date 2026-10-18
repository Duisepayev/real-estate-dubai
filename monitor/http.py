import json
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def request(url, data=None, headers=None, timeout=60, retries=3, method=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
         "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    h.update(headers or {})
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = HttpError(e.code, body)
            if e.code < 500 and e.code != 429:
                raise last
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(5 * (attempt + 1))
    raise last


def get_json(url, **kw):
    return json.loads(request(url, **kw))
