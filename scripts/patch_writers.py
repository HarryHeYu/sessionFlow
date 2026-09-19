# one-off: writers home-override + test_writers main import
from pathlib import Path

p = Path("voyager/writers.py")
s = p.read_text(encoding="utf-8")
if "VOYAGER_HOME_OVERRIDE" not in s.split("def write_transcript")[0]:
    s = s.replace(
        'def _codex_writer(store: Store, thread_id: str, members: List[Any],\n'
        '                  home: Path) -> Dict[str, Any]:',
        'def _codex_writer(store: Store, thread_id: str, members: List[Any],\n'
        '                  home: Path) -> Dict[str, Any]:\n'
        '    home = Path(os.environ.get("VOYAGER_HOME_OVERRIDE", home))', 1)
    s = s.replace(
        'def _grok_writer(store: Store, thread_id: str, members: List[Any],\n'
        '                 home: Path) -> Dict[str, Any]:',
        'def _grok_writer(store: Store, thread_id: str, members: List[Any],\n'
        '                 home: Path) -> Dict[str, Any]:\n'
        '    home = Path(os.environ.get("VOYAGER_HOME_OVERRIDE", home))', 1)
    p.write_text(s, encoding="utf-8")
    print("writers home override added")

p2 = Path("tests/test_writers.py")
s2 = p2.read_text(encoding="utf-8")
if "from voyager.cli import main" not in s2:
    s2 = s2.replace("from voyager.store import Store",
                    "from voyager.cli import main\nfrom voyager.store import Store", 1)
    p2.write_text(s2, encoding="utf-8")
    print("test import added")
