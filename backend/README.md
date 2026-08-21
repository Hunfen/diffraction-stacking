# Diffraction Stacking — 后端

FastAPI 后端（采集主机运行）：相机管理、实时预览、四相位序列拍摄（Bias/Dark/Flat/Light）、
天文式标定叠加处理管线。API 契约见 [`../docs/api.md`](../docs/api.md)。

## 目录

```
backend/
  requirements.txt
  app/
    main.py       FastAPI 入口（REST + /ws/live + /ws/events + 静态托管）
    camera.py     MockCamera（合成 LEED/RHEED 点阵）/ IC4Camera（真机）+ 设备服务
    capture.py    CaptureService 状态机与四相位拍摄、FITS/缩略图落盘
    process.py    calibrate / stack / stretch 后台任务
    sessions.py   session 元数据扫描
    util.py       通用工具（统计、自动对比度、路径）
```

## 开发环境（macOS，Mock 相机）

```bash
cd backend
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- 打开浏览器：`http://127.0.0.1:8000/docs`（Swagger UI）
- Mock 设备 `mock-1` 始终可用，macOS 无 ic4 时自动使用 mock，全流程可跑
- 端到端自测：`cd backend && .venv/bin/python ../scripts/backend_smoke.py`（退出码 0 = 通过）
- 契约 v1.1：LIGHT 相位前 `needs_user=true` 必须人工 resume；calibrate/stack 支持
  `exclude` 按文件名排除坏帧；`STACK_*.fits` 为 float32（BITPIX=-32）
- FITS 温度关键字用短名 `TEMP`（°C），避免 astropy `TEMPERATURE` 触发的 HIERARCH VerifyWarning
- Mock 性能（本机实测）：1920×1200 全分辨率帧生成约 22–24 fps（衍射场景）/
  17–24 fps（暗场场景），满足 ≥15 fps。优化口径：点阵与整数平移基底缓存、
  漂移仅做加权复用、泊松噪声混合实现（λ≥24 高斯近似 σ=√λ，0.5≤λ<24 精确泊松，
  λ<0.5 Bernoulli 近似）——与契约“泊松噪声（√signal）”一致；JPEG 编码/推流仍在独立线程，
  推送帧率还受 scale/quality 与客户端网络影响

## Windows 真机部署（DMK 33GX249e + ic4）

1. **安装驱动**：从 The Imaging Source 官网下载并安装 DMK 33GX249e 的
   [GigE 相机驱动（ic4）](https://www.theimagingsource.com/)，按向导完成网卡
   GigE Vision 过滤器绑定；建议开启 Jumbo Frame（9000 MTU）。
2. **网络**：千兆网口直连相机或接入同一交换机；在设备管理器确认相机网卡
   识别正常，ic4 自带工具（如 ic4 Device Manager）能看到设备。
3. **Python**：安装 64 位 Python 3.10+（推荐 3.12）。
4. **虚拟环境与依赖**：

   ```powershell
   cd backend
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install imagingcontrol4
   ```

5. **启动服务**：

   ```powershell
   .venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

6. **验证**：局域网浏览器打开 `http://<采集主机IP>:8000/docs`，
   `GET /api/devices` 应同时列出 `mock-1` 与 `ic4-<serial>`；
   `POST /api/device/open {"id":"ic4-<serial>"}` 打开真机后即可预览/拍摄。
7. **前端**：构建 `frontend/dist/` 后重启后端，FastAPI 自动将静态文件挂载到 `/`。

## 说明

- `imagingcontrol4` 为**懒加载**：macOS/Linux 未安装时不影响 Mock 模式运行；
  尝试打开 ic4 设备会返回清晰的中文错误。
- 数据默认写仓库根目录 `data/`（`data/snapshots/`、`data/captures/<时间戳>_<名称>/`），
  可用环境变量覆盖：`DIFF_DATA_ROOT=/path/to/data`。
