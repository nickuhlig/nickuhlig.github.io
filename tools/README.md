# tools/ — library refresh scripts

Not part of the website (`_config.yml` excludes this folder). The library they maintain is
`../projects/free-recipe-database`. Paths are derived from where this folder sits (`library_paths.py`),
so the same scripts run on the PC and on the Raspberry Pi. Needs Python 3.10+.

## What runs weekly (`weekly_refresh.py`)

`git pull` → Albertsons pipeline (sweep with both restriction-discovery passes, diff, fetch, ingredients,
restrictions, cookbooks) → regenerate `manifest.json` → commit **only** the library → `git push origin master`.

* No session headers, or Albertsons rejects them, or a network stage fails → the local steps still run on the
  existing sweep, the result is pushed, and the run exits **2** (+ a notification if `NTFY_URL` is set).
* The old sweep is never overwritten by a partial/bad one (atomic write, and a new sweep under 90% of the old
  size is parked as `albertsons_full_sweep.rejected.json`). A manifest under 99% of the old size is refused.
* Exit **1** = nothing committed or pushed; see `logs/weekly_refresh_<date>.log`.

## Session headers (`albertsons_headers.json`, git-ignored)

DevTools → Network → click a recipe → right-click the `menuservice/v2/recipe` request → Copy → *Copy as cURL (bash)*,
then `python set_headers.py` and paste (Ctrl-D to finish; or `--from-file`). Never commit this file.

## Raspberry Pi setup (once)

```bash
sudo apt install -y git python3-venv
# 1. read/write access to the repo: create a deploy key (allow write) on GitHub, then
ssh-keygen -t ed25519 -f ~/.ssh/recipe_deploy -N ""      # add recipe_deploy.pub as a deploy key with write access
git clone git@github.com:nickuhlig/nickuhlig.github.io.git ~/nickuhlig.github.io   # (use a Host alias / GIT_SSH_COMMAND for the key)
cd ~/nickuhlig.github.io && git checkout master
# 2. Python environment
python3 -m venv tools/.venv && tools/.venv/bin/pip install -r tools/requirements.txt
# 3. seed the git-ignored files: copy albertsons_headers.json and albertsons_sweep_results/ from the PC
#    (scp), or run set_headers.py on the Pi
# 4. try it by hand first
cd tools && .venv/bin/python weekly_refresh.py --no-push        # then without --no-push
# 5. schedule it
sudo cp pi/recipe-refresh.service pi/recipe-refresh.timer /etc/systemd/system/    # edit User/paths first
sudo systemctl daemon-reload && sudo systemctl enable --now recipe-refresh.timer
systemctl list-timers recipe-refresh.timer
```

Cron instead of systemd: `0 3 * * 0 cd ~/nickuhlig.github.io/tools && .venv/bin/python weekly_refresh.py >> logs/cron.log 2>&1`
(a missed run is not made up, unlike the timer's `Persistent=true`).

Set the git identity on the Pi (`git config user.name/user.email`) or commits use a placeholder.
