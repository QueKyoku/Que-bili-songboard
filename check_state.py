import sys
from pathlib import Path

sys.path.insert(0, ".")

from songboard.config import Config
from songboard.netease import NeteasePlaylistDriver, weapi_post

d = NeteasePlaylistDriver(Config.load(Path("config.json")))
res = weapi_post("/v6/playlist/detail", {"id": d.playlist_id, "n": 100, "s": 8}, d.cookie)
pl = res.get("playlist") or {}
tracks = pl.get("tracks") or []
names = [t.get("name") for t in tracks]
print(f"你的歌单《{pl.get('name')}》现在 {len(tracks)} 首：")
print("  " + " / ".join(names))
print()
print("其中 好久不见、单车、浮夸、十年 是测试期间加的。")
