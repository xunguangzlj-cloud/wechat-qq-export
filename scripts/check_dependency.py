"""检查固定微信依赖的私有接口契约，避免升级后静默失效。"""
import inspect
import re
from pathlib import Path

import wechatauto
from wechatauto import MediaDownloader, WeChatDB

lines = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
pins = [line.strip() for line in lines if line.strip().startswith("wechatauto-replica")]
assert len(pins) == 1, "微信依赖必须只有一个固定版本声明"
match = re.fullmatch(r"wechatauto-replica==([0-9A-Za-z._+-]+)", pins[0])
assert match, "微信依赖必须固定为精确版本"
assert wechatauto.__version__ == match.group(1), "微信依赖版本与声明不一致"
for name in ("_open", "_nickname_index", "extract_master_key"):
    assert hasattr(WeChatDB, name), name
for name in ("_collect_templates", "_get_xor_key", "_derive_cfg_key", "_scan_aes_key", "decrypt_image"):
    assert hasattr(MediaDownloader, name), name
assert "WECHATAUTO_KEYS_DIR" in inspect.getsource(WeChatDB._stable_key_dirs)
for name in ("_try_other_accounts", "_select_account_by_keys"):
    assert "tempfile.gettempdir()" in inspect.getsource(getattr(WeChatDB, name)), name
print("微信底层依赖契约检查通过：", wechatauto.__version__)
