# 部署到 relayB（Mac 侧一条命令）

`scripts/deploy_relayb.py` 把这台 Mac 上的仓库部署到 **relayB**（A133‑LTOmicron，
`192.168.1.101`，部署目录 `D:\LEED\static-astrophoto`，Windows 计划任务
`StaticAstroPhoto`）。它只依赖 Python 3 标准库，不需要在 Mac 上装任何包。

```bash
python3 scripts/deploy_relayb.py            # 完整部署
python3 scripts/deploy_relayb.py --dry-run  # 只读两端 SHA，打印将要执行的步骤
```

## 为什么在 Mac 侧取更新

relayB 自己拉 GitHub 不可靠：在那台机器上用 git 走 SSH 远程会 `exit 128`（ssh 本身可用，
是 git 调起 ssh 时失败），HTTPS 远程又时好时坏。而 Mac 上有可用的 GitHub 凭据。

因此把「取更新」放在 Mac：Mac 先 `git fetch` 拿到最新 `main`，再把 relayB 缺少的那些
commit 打成一个 **git bundle**（单个文件，通常只有几百 KB），用 `scp` 传过去让 relayB
用它本地已有的历史做快进合并。relayB 全程不访问 GitHub。

## 前置条件

1. `ssh relayB` 可直接登录（`~/.ssh/config` 里已有 relayB 条目，密钥无需交互口令；
   脚本用 `BatchMode=yes`，不会弹出密码提示）。
2. Mac 能访问 GitHub：先试 `git@github.com:Hunfen/diffraction-stacking.git`，
   失败自动回退 `https://github.com/Hunfen/diffraction-stacking.git`。
3. relayB 上 `backend\.venv`、Node.js 与 npm 已就绪（`scripts/install.ps1` 的产物），
   且该仓库是干净的 fast-forward 状态。

## 可选开关

| 开关 | 作用 |
| --- | --- |
| `--dry-run` | 取远端、读双方 SHA、打印完整计划后退出。**不停止服务、不写目标机任何文件**；relayB 不可达时只告警并仍然打印计划（exit 0）。 |
| `--no-restart` | 停止任务、合并、刷新依赖并重建前端，但**不重新启动** `StaticAstroPhoto`（服务保持停止，HTTP 校验跳过）。 |
| `--skip-frontend` | 跳过 `npm install` 与 `npm run build`（前端资源一致性校验也跳过）。 |
| `--keep-temp` | 保留本地 bundle、临时分支 `relayb-deploy` 与 relayB 上的临时目录，便于排查。 |

## 一次部署做了什么

1. `git rev-parse --show-toplevel`，读取本地 `HEAD` 与 `origin/main`。
2. 依次尝试 SSH / HTTPS 拉取 `main`，输出里会明确写出**这次用的是哪一条**、这一条的超时
   设置以及远端 SHA（SSH 带 `ConnectTimeout=20`，HTTPS 兜底 30 秒无数据即放弃）。
3. `ssh relayB git -C D:\LEED\static-astrophoto rev-parse HEAD` 读出目标机当前部署的 commit。
4. 两端相同 → 打印 `already up to date` 并 `exit 0`，**不重启服务**。
5. 不同 → `git branch -f relayb-deploy <远端 SHA>`，再
   `git bundle create <tmp> <部署SHA>..relayb-deploy`（增量）。
   若目标机的 commit 在本地不存在、或它不是远端 commit 的祖先，则自动回退成
   `git bundle create <tmp> relayb-deploy`（全量）。bundle 里带的是
   `refs/heads/relayb-deploy` 这个引用，而不是裸 SHA。
6. `scp` bundle 与一个生成的 `.bat` 到 relayB 的 `%TEMP%\relayb-deploy-<时间戳>\`，
   然后 `ssh relayB <那个 .bat>` 执行。`.bat` 的步骤：
   `schtasks /End /TN StaticAstroPhoto` → 轮询到 8000 端口不再监听 →
   `git fetch "<bundle>" refs/heads/relayb-deploy` → `git merge --ff-only FETCH_HEAD` →
   `backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt` →
   `cd frontend && npm install --no-audit --no-fund && npm run build` →
   `schtasks /Run /TN StaticAstroPhoto` → 轮询 8000 端口最多 60 s。
   远端命令的输出由 ssh 直接回传到本终端（**不**重定向到文件或 `/dev/null`，
   那会把该机上的 ssh 会话挂住）。
7. 健康检查：`GET /api/health` 要求 `camera_driver=ic4`；`/api/capture/sets`、
   `/api/capture/draft` 要求 200；`GET /` 引用的 `assets/index-*.js` 必须与
   relayB 上 `frontend\dist\index.html` 里的一致（证明服务端确实在跑这次构建的前端）。
8. 清理本地临时目录与 `relayb-deploy` 分支、以及 relayB 上的临时目录（`--keep-temp` 可保留）。

任何一步失败都会以 **非 0** 退出，并打印失败的步骤、失败命令、退出码与最后 30 行输出。

## 失败之后

- 想看完整现场：加 `--keep-temp` 重跑一次（或从失败输出里记下 `relayB:<临时目录>`），
  bundle 与 `.bat` 都还在那里。
- 手工兜底（在 relayB 上）：
  ```cmd
  schtasks /End /TN StaticAstroPhoto
  git -C D:\LEED\static-astrophoto fetch "<临时目录>\repo.bundle" refs/heads/relayb-deploy
  git -C D:\LEED\static-astrophoto merge --ff-only FETCH_HEAD
  ```
  之后 `schtasks /Run /TN StaticAstroPhoto` 即可。
- `--no-restart` 之后服务是停着的，需要手动 `schtasks /Run /TN StaticAstroPhoto`。

## 已知限制

- **只做 fast-forward**：relayB 上的部署目录若被本地改动或分叉，`merge --ff-only`
  会失败而不是覆盖现场；本脚本不做 reset、不回滚。
- 服务停机窗口 = 整个 `.bat` 的执行时间（含 `npm install` 与 `npm run build`，
  通常一到数分钟）。
- 远端临时目录路径按原样（不加引号）传给 `cmd.exe`；若 `%TEMP%` 含空格，脚本会直接报错退出。
- 临时分支名固定为 `relayb-deploy`；若本地已存在同名分支，脚本会复用它并在结束时保留它。
- 两条 GitHub 路径都不可用时，脚本在取更新这一步就失败（`--dry-run` 也一样），因为计划本身
  需要远端 SHA。超时已收紧：SSH 侧带 `ConnectTimeout=20`，HTTPS 兜底若 30 秒内没有数据
  （`http.lowSpeedLimit=1` + `http.lowSpeedTime=30`）就放弃，不再出现「卡住几分钟没有输出」。
  两路都失败时不会进入远端部署；若失败发生在 `.bat` 执行途中（计划任务已被停掉），
  服务保持停止，用 `ssh relayB schtasks /Run /TN StaticAstroPhoto` 恢复。

## 本地校验

```bash
python3 -m py_compile scripts/deploy_relayb.py
python3 scripts/deploy_relayb.py --help
python3 scripts/deploy_relayb.py --dry-run
```

> 在 macOS 上用 `/usr/bin/python3` 跑 `-m py_compile` 时，Apple 版解释器会把字节码写到
> `~/Library/Caches/com.apple.python`（工作区沙箱外）；若该路径不可写，改用
> `PYTHONPYCACHEPREFIX=$PWD/.pycache python3 -m py_compile scripts/deploy_relayb.py`
> 或 `python3 -c "import ast;ast.parse(open('scripts/deploy_relayb.py').read())"`。
