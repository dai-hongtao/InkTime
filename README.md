# InkTime · 墨水屏回忆相框

<p align="left">
  <img src="esp32/InkTime.jpeg" width="80%">
</p>

InkTime 是一个「拉回你相册里的记忆」的墨水屏电子相框项目。

它不会随机展示照片，也不是简单的按时间轴播放，而是：

- 用 AI 理解每一张照片在“拍什么”
- 给照片打「值得回忆度 / 美观度」分
- 写一句灵光一现的旁白文案
- 每天选出"历史上的今天"，**最值得被再次看到的照片**
- 推送到 ESP32 墨水屏上展示

---
## 项目整体结构

InkTime 分为三部分：

1. **照片分析（Python）**  
   扫描相册 → 调用视觉模型 → 分类 / 评分 / 写文案 → 存入数据库


2. **图片渲染（Python）**  
   从数据库里选出「历史上的今天」高分照片 → 渲染成 ESP32 可直接显示的 `.bin`


3. **下载与展示（ESP32）**  
   ESP32 定时从服务器拉取 `.bin` → 刷新墨水屏 → 深度休眠直至下次唤醒

---
## 环境准备

### 1）Python
推荐 Python 3.10+。

建议使用虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2）安装 exiftool （可选）
InkTime 可以在不装 exiftool的情况下运行，但不一定能完整地获取 EXIF 中的 GPS 信息。

建议使用 exiftool 获取 GPS 信息：  

MacOS(Homebrew): ```brew install exiftool```  
Linux: ```sudo apt-get install -y libimage-exiftool-perl```

### 3) 配置 config.py
```
cp config-example.py config.py  
vi config.py
```
必须配置以下字段：  
照片库路径 ```IMAGE_DIR```  
VLM 模型接口 ```API_URL``` ```MODEL_NAME```  
InkTime 使用 OpenAI 接口（LM Studio / 其它兼容服务均可）。

为防止照片隐私泄露，建议修改```DOWNLOAD_KEY```，为 ESP32 下载路径加一个随机前缀作为密钥。   
同时，请同步修改```esp32/ink-display-7C-photo/ink-display-7C-photo.ino```固件中的```DAILY_PHOTO_PATH_PREFIX```字段。

## 分析照片
分析照片前，请先确保：
- LM Studio（或你的云端 VLM 服务）已启动
- config.py 已正确配置

执行：

```python3 analyze_photos.py```

视觉大模型会读取并理解相册目录中的所有文件，为每张照片生成：

- 画面描述
- 照片类型
- 值得回忆度 / 画面美观度评分
- 一句话文案

图片数据会保存在```photos.db```中（SQLite数据库）。

你可以修改```analyze_photos.py```中的提示词，以微调模型的评价标准。

程序可以断点续跑，已处理过的照片信息不会重复分析，你可以分几天分析完你的整个相册。

*请根据你拥有的算力选择合适的模型，作者使用的 qwen3-vl-30b 已经能取得相当不错的文案。*

## 为 ESP32 渲染"历史上的今天"照片
执行：

```python3 render_daily_photo.py```

## 启动 ESP32 下载服务器和 WebUI
执行：

```python3 server.py```

#### WebUI（如果开启）：
Server 将提供一个简明的可视化前端，用于查看已处理照片的描述、文案，并预览模拟墨水屏渲染效果。

在浏览器中访问：

```http://127.0.0.1:8765/review```

程序跑通后，建议在```config.py```中关闭WebUI，仅保留 ESP32 下载接口。

## 服务器部署与定时任务示例（可选）

创建 systemd 服务：

```sudo vi /etc/systemd/system/inktime-server.service```

示例（请自行修改项目路径）：

```
[Unit]
Description=InkTime Server
After=network.target

[Service]
Type=simple
# 改成你的项目路径
WorkingDirectory=/path/to/InkTime
ExecStart=/path/to/InkTime/venv/bin/python server.py
Restart=always
RestartSec=3
User=inktime
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable inktime-server
sudo systemctl start inktime-server
```

使用 crontab 每天凌晨自动选片、渲染：

```
chmod +x scripts/daily_render.sh
sudo -u inktime crontab -e
0 5 * * * /path/to/InkTime/scripts/daily_render.sh
```

在 ```logs/render.log```可查看日志。

---

# ESP32 墨水屏硬件部分

## 硬件与引脚
#### 主控
本项目使用乐鑫 ESP32-S3-N8R8 模块。  
当然，你也可以使用任何成品 ESP32 开发板进行制作。  
如使用其它开发板或模块，请注意选择带 PSRAM 的型号（需至少 384K PSRAM）。  
#### 屏幕
本项目使用 7.3 寸四色墨水屏，型号为 EL073TS3。使用 GxEPD2 库驱动（GxEPD2_730c_GDEY073D46）。  
其它尺寸、型号请自行参照 GxEPD2 库中的硬件支持列表修改构造函数。
#### 墨水屏转接板
本项目使用 B 站"记得带马扎"制作的七色 EPD 墨水屏转接板。  
但市面上的大部分 24pin SPI 转接板均可兼容。

#### 引脚定义
墨水屏使用 SPI 通信，本项目默认引脚为：
- `PIN_EPD_BUSY = 14`
- `PIN_EPD_RST  = 13`
- `PIN_EPD_DC   = 12`
- `PIN_EPD_CS   = 11`
- `PIN_EPD_SCLK = 10`
- `PIN_EPD_DIN  = 9`

### 主板焊接
原理图、BOM清单、制板文件均位于```esp32/pcb```文件夹中。  
原理图中的 H1 - H6 为测试焊盘引出，无需焊接真实器件：
- H1: UART 串口
- H2: USB
- H3: BOOT引脚，烧录固件时需将改引脚短接到 GND 后上电
- H4: 焊接至 EPD 墨水屏转接板
- H5: 3.7V 电池焊盘
- H6: 5V 输入测试焊盘

建议使用 UART 串口烧录固件。R2、R3、C5、C6 供 USB 使用，如无需要，可留空不焊。

SW1：RESET 键，按下后会重启设备，并从服务器拉取、显示图片一次。RESET 键可将设备从长休眠状态中唤醒。  
SW2：WiFi 重置键，按住 SW2 再按下 SW1，ESP32 重启后会清空 NVS，以重新配置 WiFi 连接。  
SW3 / SW4: 备用 GPIO，以防未来需要添加的功能。如无需要，可留空不焊。

完整 PCB 板示例：

<p align="left">
  <img src="esp32/pcb/pcb.jpeg" width="80%">
</p>

## 编译与烧录

建议使用 Arduino IDE。

1. 安装 ESP32 Arduino Core。
2. 选择开发板：ESP32-S3（必须开启 PSRAM）。
3. 安装依赖库：
   - `GxEPD2`
4. 打开并编译/烧录 `ink-display-7C.ino`。

### 自定义字体(可选)
如需使用自定义中文字体，可放入 ```resource/fonts/``` 中，并在```config.py```中声明。

## 首次配置

设备启动时，会尝试从 NVS 读取已保存的 Wi-Fi 配置；若未配置或 Wi-Fi 连接失败，会自动进入 AP 配置模式：

- 设备会开启 AP 热点：`InkTime-xxxx`
- 默认密码：`12345678`
- 连接 AP ，用浏览器访问配置页面：`http://192.168.4.1/`
- 配置 Wi-Fi、服务器地址、定时更新时间并保存，设备会自动重启并进入正常工作流程。

## 刷新与休眠

- 设备每天会在配置的更新时间，从服务器拉取一次当日生成的图片，并刷新墨水屏。
- 成功刷新后，会进入 Deep Sleep，直到下一次被唤醒。
- 若下载超时（默认 60s），也会进入长休眠，避免异常耗电。
- 在任意时候，按下 RESET 键，会强制重启并马上拉取、刷新一次图片。
- 长休眠待机电流 ＜ 1mA，如使用 2 节 18650 电池，5000mAh 约可实现半年续航。