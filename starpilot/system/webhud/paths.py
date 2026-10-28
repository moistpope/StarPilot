"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Where the web HUD bridge's files live. The page itself is Oceanfront View
(https://github.com/moistpope/oceanfront-view): its Android app carries it onto the car's screen and
serve.py in that checkout serves it to a browser, so the bridge serves only what is its own, the DBCs
under /dbc/ (the ADASBUS subset it streams by, under third_party/webhud to stay out of lint,
spellcheck and size limits; the radar's from opendbc). Set WEBHUD_PAGE_DIR to an Oceanfront View
checkout and the bridge serves the page as well, for a plain browser on a PC.
"""
import os

from opendbc import DBC_PATH as OPENDBC_DBC_PATH
from openpilot.common.basedir import BASEDIR

WEBHUD_DIR = os.path.dirname(os.path.abspath(__file__))
THIRD_PARTY_DIR = os.path.join(BASEDIR, "third_party", "webhud")
DBC_PATH = os.path.join(THIRD_PARTY_DIR, "dbc", "fisker_ocean_adas_world.dbc")
RADAR_DBC_PATH = os.path.join(OPENDBC_DBC_PATH, "fisker_ocean_mrr.dbc")   # reverse-engineered, see its comments


def page_roots(page_dir: str) -> dict[str, str]:
  """URL prefix -> directory for an Oceanfront View checkout ("" = the page itself): the app's own layout."""
  return {
    "vendor/": os.path.join(page_dir, "third_party", "three"),
    "models/": os.path.join(page_dir, "third_party", "models"),
    "dbc/": os.path.join(page_dir, "dbc"),
    "": os.path.join(page_dir, "static"),
  }


PAGE_DIR = os.environ.get("WEBHUD_PAGE_DIR") or None
# URL prefix -> directory served under it; without a page, only the bridge's own DBCs
STATIC_ROOTS = page_roots(PAGE_DIR) if PAGE_DIR else {"dbc/": os.path.join(THIRD_PARTY_DIR, "dbc")}
STATIC_DIR = STATIC_ROOTS.get("")   # None: no page here, and "/" says where it is
# single files served at a URL of their own: the page decodes the radar with the DBC kept in opendbc
STATIC_FILES = {
  "dbc/fisker_ocean_mrr.dbc": RADAR_DBC_PATH,
}
