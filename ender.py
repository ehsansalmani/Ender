#!/usr/bin/env python3
"""
ender.py - جمع‌آوری اندپوینت‌ها با gau, waybackurls, katana, katana (js) و linkfinder

مثال‌ها:
    python ender.py -u example.com
    python ender.py -u example.com -o out.txt
    python ender.py -list domains.txt -o out.txt
    python ender.py -set gau no
    python ender.py -set all yes
    python ender.py -show
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

CONFIG_FILE = Path(__file__).resolve().parent / "endpoints_tools.txt"

DEFAULT_TOOLS = {
    "gau": "yes",
    "wayback": "yes",
    "katana": "yes",
    "katana_js": "yes",
    "linkfinder": "yes",
}

LINKFINDER_CANDIDATES = ("linkfinder", "linkfinder.py")
STATIC_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".css", ".mp4", ".mp3", ".webp",
)

print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def ensure_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            "".join(f"{t} {s}\n" for t, s in DEFAULT_TOOLS.items()),
            encoding="utf-8",
        )


def load_config():
    ensure_config()
    config = dict.fromkeys(DEFAULT_TOOLS, False)
    seen = set()

    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        tool, state = parts[0].lower(), parts[1].lower()
        if tool in config and state in ("yes", "no"):
            config[tool] = state == "yes"
            seen.add(tool)

    # ابزارهای جدیدی که در فایل قدیمی نیستند، پیش‌فرض خودشان را می‌گیرند
    for tool in DEFAULT_TOOLS:
        if tool not in seen:
            config[tool] = DEFAULT_TOOLS[tool] == "yes"
    return config


def save_config(config):
    CONFIG_FILE.write_text(
        "".join(f"{t} {'yes' if config[t] else 'no'}\n" for t in DEFAULT_TOOLS),
        encoding="utf-8",
    )


def set_config(tool, state):
    tool, state = tool.lower(), state.lower()
    if tool != "all" and tool not in DEFAULT_TOOLS:
        raise ValueError(
            f"Unknown tool: {tool} (choose from: {', '.join(DEFAULT_TOOLS)}, all)"
        )
    if state not in ("yes", "no"):
        raise ValueError("State must be yes or no")

    config = load_config()
    for name in (DEFAULT_TOOLS if tool == "all" else (tool,)):
        config[name] = state == "yes"
    save_config(config)
    print(f"[+] {tool} set to {state}")


def show_config():
    config = load_config()
    for tool in DEFAULT_TOOLS:
        print(f"{tool:<12} {'yes' if config[tool] else 'no'}")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def build_env():
    env = os.environ.copy()
    entries = env.get("PATH", "").split(os.pathsep)
    extra = [str(Path.home() / "go" / "bin"), str(Path.home() / ".local" / "bin")]
    for item in env.get("GOPATH", "").split(os.pathsep):
        if item:
            extra.append(str(Path(item) / "bin"))
    for p in extra:
        if p not in entries:
            entries.append(p)
    env["PATH"] = os.pathsep.join(entries)
    return env


def valid_domain(domain):
    if len(domain) > 253 or "." not in domain:
        return False
    return all(
        1 <= len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in domain.split(".")
    )


def normalize_target(value):
    """example.com / https://example.com/path / example.com:8080 -> example.com"""
    value = value.strip().lower()
    if not value:
        return None
    if "://" not in value:
        value = "//" + value
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return None
    host = host.rstrip(".")
    return host if valid_domain(host) else None


def in_scope(url, domain):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and (
        host == domain or host.endswith("." + domain)
    )


def find_linkfinder(env):
    custom = os.environ.get("LINKFINDER")
    if custom:
        parts = custom.split()
        exe = shutil.which(parts[0], path=env["PATH"])
        if exe:
            return [exe, *parts[1:]]
    for name in LINKFINDER_CANDIDATES:
        exe = shutil.which(name, path=env["PATH"])
        if exe:
            # linkfinder.py ممکن است executable نباشد
            if name.endswith(".py"):
                return [sys.executable, exe]
            return [exe]
    return None


def is_js(url):
    return urlsplit(url).path.lower().endswith(".js")


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------
def run_stream_tool(name, args, domain, urls, env, timeout):
    """ابزارهایی که هر خط خروجی‌شان یک URL است (gau, waybackurls, katana)."""
    exe = shutil.which(args[0], path=env["PATH"])
    if exe is None:
        log(f"[-] {name}: command '{args[0]}' not found in PATH")
        return False

    log(f"[*] Running {name}: {' '.join(args)}")
    before = len(urls)
    timer = None

    try:
        proc = subprocess.Popen(
            [exe, *args[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if timeout:
            timer = threading.Timer(timeout, proc.kill)
            timer.start()

        with proc:
            for raw in proc.stdout:
                url = raw.strip()
                if url and in_scope(url, domain):
                    urls.add(url)
            code = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        raise
    except OSError as error:
        log(f"[-] {name}: {error}")
        return False
    finally:
        if timer:
            timer.cancel()

    log(f"[+] {name}: {len(urls) - before} new URLs (total: {len(urls)})")
    if code != 0:
        log(f"[-] {name} exited with code {code}")
        return False
    return True


LF_ENDPOINT = re.compile(r"^(?:https?://|//|/|\.\./|\./)?[\w\-./%~:@?&=+#,;\[\]]+$")


def linkfinder_one(lf_cmd, js_url, env, timeout):
    try:
        result = subprocess.run(
            [*lf_cmd, "-i", js_url, "-o", "cli"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    found = []
    for line in result.stdout.splitlines():
        line = line.strip().strip("'\"")
        if (
            not line
            or line.startswith("Running against")
            or " " in line
            or not LF_ENDPOINT.match(line)
        ):
            continue
        if line.lower().endswith(STATIC_EXT):
            continue
        found.append(urljoin(js_url, line))
    return found


def run_linkfinder(domain, urls, env, threads, timeout, max_js):
    lf_cmd = find_linkfinder(env)
    if lf_cmd is None:
        log(
            "[-] linkfinder: not found in PATH "
            "(pip install linkfinder, or set LINKFINDER='python3 /path/linkfinder.py')"
        )
        return False

    js_files = sorted(u for u in urls if is_js(u))
    if max_js:
        js_files = js_files[:max_js]
    if not js_files:
        log("[*] linkfinder: no JS files found, skipping")
        return True

    log(f"[*] Running linkfinder on {len(js_files)} JS files ({threads} threads)")
    before = len(urls)
    done = 0

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(linkfinder_one, lf_cmd, js, env, timeout) for js in js_files
        ]
        try:
            for fut in as_completed(futures):
                done += 1
                for url in fut.result():
                    if in_scope(url, domain):
                        urls.add(url)
                if done % 25 == 0:
                    log(f"    linkfinder: {done}/{len(js_files)} JS done")
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise

    log(f"[+] linkfinder: {len(urls) - before} new URLs (total: {len(urls)})")
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def scan_domain(domain, config, env, opts):
    urls = set()
    failed = False

    stream_tools = [
        ("gau", ["gau", domain]),
        ("wayback", ["waybackurls", domain]),
        ("katana", ["katana", "-u", domain, "-silent"]),
        ("katana_js", ["katana", "-u", domain, "-jc", "-em", "js", "-silent"]),
    ]

    for name, cmd in stream_tools:
        if config[name] and not run_stream_tool(
            name, cmd, domain, urls, env, opts.timeout
        ):
            failed = True

    if config["linkfinder"] and not run_linkfinder(
        domain, urls, env, opts.threads, opts.lf_timeout, opts.max_js
    ):
        failed = True

    return urls, failed


def read_targets(args):
    raw = []
    if args.url:
        raw.extend(args.url)
    if args.list:
        path = Path(args.list)
        if not path.is_file():
            sys.exit(f"[-] List file not found: {path}")
        raw.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())

    targets, seen = [], set()
    for item in raw:
        if not item.strip() or item.strip().startswith("#"):
            continue
        domain = normalize_target(item)
        if domain is None:
            log(f"[-] Skipping invalid target: {item.strip()}")
            continue
        if domain not in seen:
            seen.add(domain)
            targets.append(domain)
    return targets


def build_parser():
    p = argparse.ArgumentParser(
        description="Collect endpoints with gau, waybackurls, katana, katana-js and linkfinder"
    )
    p.add_argument("-u", "--url", action="append", help="Target domain (can repeat)")
    p.add_argument("-l", "-list", "--list", dest="list", help="File with one domain per line")
    p.add_argument("-o", "--output", help="Write results to this file")
    p.add_argument(
        "-set", dest="set", nargs=2, metavar=("TOOL", "yes|no"),
        help=f"Enable/disable a tool: {', '.join(DEFAULT_TOOLS)}, all",
    )
    p.add_argument("-show", action="store_true", help="Show tool states")
    p.add_argument("-t", "--threads", type=int, default=10, help="linkfinder threads (default 10)")
    p.add_argument("--timeout", type=int, default=0, help="Per-tool timeout in seconds (0 = none)")
    p.add_argument("--lf-timeout", type=int, default=60, help="linkfinder timeout per JS file")
    p.add_argument("--max-js", type=int, default=0, help="Max JS files for linkfinder (0 = all)")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.show:
        show_config()
        return 0

    if args.set:
        try:
            set_config(*args.set)
        except ValueError as error:
            parser.error(str(error))
        return 0

    if not args.url and not args.list:
        parser.error("one of -u or -list is required")

    targets = read_targets(args)
    if not targets:
        parser.error("no valid targets")

    config = load_config()
    if not any(config.values()):
        log("[-] All tools are disabled. Use: -set all yes")
        return 1

    env = build_env()
    all_urls, failed = set(), False

    try:
        for i, domain in enumerate(targets, 1):
            log(f"\n[#] ({i}/{len(targets)}) {domain}")
            urls, f = scan_domain(domain, config, env, args)
            all_urls |= urls
            failed = failed or f
    except KeyboardInterrupt:
        log("\n[!] Interrupted - saving what was collected so far")
        failed = True

    result = "".join(f"{u}\n" for u in sorted(all_urls))
    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        log(f"[+] Saved to {args.output}")
    else:
        sys.stdout.write(result)

    log(f"[+] Finished: {len(all_urls)} unique URLs")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
