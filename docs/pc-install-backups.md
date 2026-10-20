# PC installation artifact backups

GitHub stores project code. PC install packages and restore materials are also retained under
`E:\RIGOON_STUDIO\PC_INSTALL_BACKUPS\resource-analyzer\<date-time>` at installation/update,
usable build and completion milestones. Existing unrelated project backup locations remain unchanged.

From this repository, run:

```powershell
python tools/backup_installables.py --outputs-root <project-output-directory> --sensor-archive <LibreHardwareMonitor.zip>
```

The command backs up allowlisted installation artifacts from `ResourceAnalyzer-v*` and `NVIDIA-Studio-*`
output folders, the complete sensor bridge/dependency package, the optional upstream sensor ZIP and local
Codex working instructions. It also creates and verifies a Git source bundle for offline restoration.
The portable analyzer executable includes its Python runtime and browser assets; it does not require a local ML model.

Every copied file is verified by size and SHA-256. Equal content is verified and reused from an E-drive object
store; snapshots link to E-drive objects where supported, with verified copies as fallback. No links point back
to C. Modified files and source commits receive new snapshots. Earlier snapshots are retained. A manifest
records verification results and source commit; `RESTORE.txt` gives restoration steps and `LATEST.txt` points
to the newest completed snapshot. C-drive originals and running apps are untouched.

An interrupted or failed snapshot is incomplete and does not replace the latest verified pointer. No mirror,
purge, app shutdown/restart, background service or scheduled task is performed. This is a milestone procedure,
not a claim that backups run outside active work.

This artifact backup excludes measurement databases, screenshots, account credentials, `.env` files, customer
data and original audio/media. They require their own agreed backup scope. Backup files and manifests stay
on E and must not be added to public GitHub. Copy snapshot files into a working directory before modifying them.
