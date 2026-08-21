# Diffraction Stacking

面向 LEED / RHEED 静态电子衍射点的采集与处理系统。

- 相机：The Imaging Source DMK 33GX249e（单色 12-bit GigE CMOS）
- 方法：借用深空摄影的 Bias / Dark / Flat 标定 + 多帧 Sigma 叠加，无跟踪需求
- 架构：FastAPI 后端（Windows 采集主机）+ React Web 前端（局域网浏览器访问）
- 数据链：12-Bit Packed 传输 → uint16 内存 → 16-bit FITS 落盘

详见 [docs/architecture.md](docs/architecture.md) 与 [docs/api.md](docs/api.md)。
