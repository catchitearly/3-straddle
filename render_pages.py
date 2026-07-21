"""
Rebuilds output/index.html (backtest dashboard) and output/live.html (live
notifier status) together from whatever is currently committed in the repo -
data/live_state.json and output/results.json.

Both .github/workflows/backtest.yml and .github/workflows/live_notifier.yml
run this right before uploading the Pages artifact. GitHub Pages serves one
artifact at a time - if each workflow only regenerated its own page, every
deploy would silently delete the other one. Running this in both workflows
means every deploy always contains both pages, whichever triggered it.
"""

import json
import os

import config
from src.dashboard_generator import generate_dashboard
from src.live_dashboard import generate_live_page

LIVE_STATE_PATH = "data/live_state.json"


def main():
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    if os.path.exists(config.RESULTS_JSON):
        with open(config.RESULTS_JSON, "r") as f:
            day_results = json.load(f)
        generate_dashboard(day_results, config.OUTPUT_HTML)
        print(f"Regenerated {config.OUTPUT_HTML} from {config.RESULTS_JSON}")
        index_exists = True
    else:
        # No backtest has ever been run - still write a minimal index.html so
        # the live.html nav link isn't dead, and so the Pages artifact isn't empty.
        with open(config.OUTPUT_HTML, "w") as f:
            f.write(
                "<!DOCTYPE html><html><body style='background:#0f1115;color:#8a90a0;"
                "font-family:sans-serif;padding:24px;'>"
                "No backtest has been run yet. "
                "<a href='live.html' style='color:#4f8cff;'>View live status &rarr;</a>"
                "</body></html>"
            )
        print(f"No {config.RESULTS_JSON} yet - wrote placeholder {config.OUTPUT_HTML}")
        index_exists = False

    if os.path.exists(LIVE_STATE_PATH):
        with open(LIVE_STATE_PATH, "r") as f:
            state = json.load(f)
    else:
        state = None

    live_path = os.path.join(config.OUTPUT_DIR, "live.html")
    generate_live_page(state, live_path, backtest_index_exists=index_exists)
    print(f"Regenerated {live_path}")


if __name__ == "__main__":
    main()
