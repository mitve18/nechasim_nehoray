"""Postgres אמיתי לבדיקות SQL, בלי Neon ובלי סודות: PGlite (Postgres שרץ ב-Node) עם pgvector.

דורש py-pglite (pip install py-pglite) ו-Node.js. בלי אחד מהם, postgres() מחזיר None והבדיקות שתלויות בו מדלגות.
"""
import contextlib
import shutil
import subprocess
from pathlib import Path

PORT = 54329
WORK = Path(__file__).parent / ".pglite"  # node_modules של PGlite, מוחרג ב-.gitignore


@contextlib.contextmanager
def postgres():
    try:
        import psycopg
        from py_pglite import PGliteConfig, PGliteManager
    except ImportError:
        yield None
        return
    if not shutil.which("npm"):
        yield None
        return
    config = PGliteConfig(use_tcp=True, tcp_port=PORT, extensions=["pgvector"], work_dir=WORK, timeout=120,
                          auto_install_deps=False)
    manager = PGliteManager(config)
    if not (WORK / "node_modules").exists():
        manager._setup_work_dir()  # כותב package.json
        # py-pglite מריץ npm בלי shell, וב-Windows זה נכשל (npm.cmd). לכן ההתקנה נעשית כאן
        subprocess.run("npm install --silent", cwd=WORK, shell=True, check=True, timeout=300)
    with manager:
        with psycopg.connect(f"postgresql://postgres:postgres@127.0.0.1:{PORT}/postgres?sslmode=disable",
                             autocommit=True) as conn:
            yield conn
