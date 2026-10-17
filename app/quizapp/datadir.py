"""データの場所（NAS の HDD 上のフォルダ）の確認。

HDD がマウントされる前に Docker がコンテナを起動すると、Docker は空のフォルダを
本体の SD カード側に作ってつないでしまう。そのまま動き出すと空のデータベースが作られ、
「問題も記録も消えた」ように見えるうえ、後でどちらが本物か分からなくなる。

そこで、初回の導入時にデータのフォルダへ目印ファイル（MARKER）を置いておき、
起動時に目印がなければ何も書かずに終了する。Docker の restart 設定により少し後で
再起動され、HDD がマウントされていればそのときに正常に起動する。
"""

from __future__ import annotations

import logging
import os
import sys
import time

MARKER = ".ai-quiz-data"

log = logging.getLogger("datadir")


class DataDirMissing(RuntimeError):
    pass


def check(data_dir: str) -> None:
    """目印ファイルがなければ DataDirMissing。"""
    marker = os.path.join(data_dir, MARKER)
    if not os.path.isfile(marker):
        raise DataDirMissing(
            f"データの場所 {data_dir} に目印ファイル {MARKER} がありません。"
            "HDD がマウントされているか、README の「データの場所を用意する」を済ませたか確認してください。"
        )
    if not os.access(data_dir, os.W_OK):
        raise DataDirMissing(f"データの場所 {data_dir} に書き込めません（フォルダの持ち主を確認してください）")


def ensure_or_exit(data_dir: str, *, skip: bool = False, wait_seconds: float = 15) -> None:
    """起動時に呼ぶ。目印がなければ少し待ってから終了コード 1 で終わる（Docker が再起動する）。"""
    if skip:
        return
    try:
        check(data_dir)
    except DataDirMissing as exc:
        log.error("%s 起動を中止します。", exc)
        print(f"[ai-quiz] {exc} 起動を中止します。", file=sys.stderr, flush=True)
        time.sleep(wait_seconds)  # すぐに再起動を繰り返さないように少し待つ
        sys.exit(1)
