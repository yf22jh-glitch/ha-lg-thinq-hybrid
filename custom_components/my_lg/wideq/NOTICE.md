# wideq — vendored library

This `wideq/` directory is a **vendored copy** of the LG ThinQ (V1/V2 "thinq2")
client used by the Home Assistant custom integration
[`ollo69/ha-smartthinq-sensors`](https://github.com/ollo69/ha-smartthinq-sensors),
which itself derives from the original
[`sampsyo/wideq`](https://github.com/sampsyo/wideq) library.

It is bundled here so this integration can read fields the official LG ThinQ
Connect (PAT) API does not expose (e.g. air-conditioner realtime power
`airState.energy.onCurrent`, cumulative energy, dehumidifier water-tank).

All credit for this client goes to the upstream authors. Please refer to the
upstream projects for licensing. This copy is used unmodified where possible;
any local changes are noted in comments.

Upstream updates use `scripts/wideq-upstream.json` as their exact base and
`scripts/sync_wideq.py` to create a separate three-way merge candidate.
Local additions, including `web_settings.py`, and the lazy SSL initialization
and account-setting request methods in `core_async.py` must be preserved.
Overlapping edits and delete/modify conflicts stop the update for review.
Do not replace this directory directly with an upstream `rsync --delete`.
