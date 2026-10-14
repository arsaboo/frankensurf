"""End-to-end check of the optional ShardX plugin (contrib/frankensurf-shardx), the way a user runs
it: installed with pip, trusted with `frankensurf plugins trust`, then named on reads.

Run only on a throwaway machine (.github/workflows/shardbench.yml with plugin_check): ShardX
downloads a closed, prebuilt Chromium from the vendor the first time it starts.

Exits non-zero unless the plugin loaded as trusted and read most of the pages itself.
"""
import asyncio
import json
import sys

# A plain page, and three Akamai-protected pages ShardX read in the 14 October benchmark.
PAGES = [
    "https://docs.python.org/3/library/asyncio.html",
    "https://www.kohls.com/search.jsp?search=blender",
    "https://www.sephora.com/search?keyword=lipstick",
    "https://www.zalando.co.uk/catalog/?q=trainers",
]


async def main() -> int:
    from frankensurf import Runtime, plugin_admin
    status = plugin_admin.status()
    loaded = [item["id"] for item in status["loaded"]]
    print(json.dumps({"plugins_loaded": loaded, "rejected": status["rejected"]}))
    if "shardx" not in loaded:
        print("shardx did not load as a trusted plugin")
        return 1
    read_by_shardx = 0
    async with Runtime(state_dir="/tmp/shardx-plugin-state") as web:
        for url in PAGES:
            result = await web.read(url, provider="shardx")
            receipt = result["receipt"]
            ok = receipt["status"] == "observed" and receipt.get("method") == "shardx"
            read_by_shardx += ok
            print(json.dumps({"url": url, "status": receipt["status"], "method": receipt.get("method"),
                              "failure": (receipt.get("failure") or {}).get("code"),
                              "chars": len(result.get("text") or "")}))
    print(f"read through the plugin: {read_by_shardx}/{len(PAGES)}")
    return 0 if read_by_shardx >= 3 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
