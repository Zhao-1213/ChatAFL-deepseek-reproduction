# ChatAFL 复现记录（DeepSeek 适配版）

> 论文：**Large Language Model guided Protocol Fuzzing**（NDSS 2024）
> 原仓库：https://github.com/ChatAFLndss/ChatAFL
> 本文档记录一次**在 Windows 笔记本上、用 DeepSeek 替代 OpenAI、完整跑通 ChatAFL** 的复现全过程。

---

## 0. 概述

| 项目 | 内容 |
|---|---|
| 被测工具 | ChatAFL（LLM 引导的协议模糊测试器，基于 AFLNet） |
| 本次改动 | 用 **DeepSeek API** 替代 **OpenAI API**（`chat-llm.c` 打补丁） |
| 环境 | Windows 10/11 + WSL2 + Ubuntu 22.04 + Docker |
| 实验规模 | 3 个被测程序 × 2 个 fuzzer，各 30 分钟 × 1 次重复 |
| 结论 | ✅ 端到端复现成功；pure-ftpd/live555 上观察到 "ChatAFL > AFLNet" 的核心趋势 |

**三大 LLM 策略**（论文核心，也是本次复现验证的重点）：

| 策略 | 作用 | 结果产物 |
|---|---|---|
| ① 语法提取（SA） | LLM 生成协议语法 → 结构感知变异 | `protocol-grammars/` |
| ② 种子扩充（SB） | LLM 补全缺失的消息类型 | `queue/` 里 `orig:enriched_*` |
| ③ 平台期突破（SC） | LLM 在卡壳时建议下一条消息 | `stall-interactions/` |

---

## 1. 环境准备

### 1.1 系统与硬件

- Windows 10/11（build ≥ 22000，需支持 WSL2）
- 本复现用的是一台普通笔记本（6G 显存），**模糊测试不占显存**，只吃 CPU/内存/磁盘

### 1.2 安装 WSL2 + Ubuntu 22.04

> ⚠️ 论文环境是 Ubuntu 20.04，但 20.04 已 EOL（apt 源失效），实测 **22.04 完全可用**。

```powershell
# PowerShell（管理员）
wsl --install --no-distribution     # 只装 WSL，不装默认发行版
# 重启电脑
wsl --update                        # 更新 WSL 到最新（本机为 3.0.1）

# 指定安装到 E 盘（C 盘空间不足时）
wsl --install Ubuntu-22.04 --location E:\WSL\Ubuntu-22.04
```

**踩坑**：`wsl --install <distro>` 会从 GitHub 拉取发行版列表，国内直连会 403/超时。两种解法：
1. 挂 VPN 后重试；
2. 从国内镜像手动下载 `.wsl` 文件，用 `wsl --install --from-file <文件> --location <路径>` 安装。

### 1.3 配置代理（VPN）

国内访问 GitHub / Docker Hub / api.openai.com 需要代理。本机代理是 Clash，监听 `127.0.0.1:7897`（Windows 侧）。

在 WSL 里，Windows 宿主 IP 是默认网关（本机为 `172.29.64.1`）。把代理写进 `~/.bashrc`：

```bash
HOST_IP=$(ip route show default | awk '{print $3}')
echo "export HOST_IP=$HOST_IP" >> ~/.bashrc
echo 'export http_proxy="http://$HOST_IP:7897"' >> ~/.bashrc
echo 'export https_proxy="http://$HOST_IP:7897"' >> ~/.bashrc
echo 'export all_proxy="http://$HOST_IP:7897"' >> ~/.bashrc
source ~/.bashrc

# 验证：应返回 HTTP/2 200
curl -I https://github.com
```

> 注意：代理软件（Clash）需开启"允许局域网连接 (Allow LAN)"，否则 WSL 连不上 `172.29.64.1:7897`。

### 1.4 安装 Docker

```bash
sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker $USER    # 加入 docker 组
sudo service docker start        # 启动 docker

# ⚠️ usermod 之后必须"退出终端重进"（或 newgrp docker）才免 sudo
docker ps                        # 验证免 sudo
```

---

## 2. 获取代码 + 安装依赖

```bash
sudo apt install -y git python3 python3-pip
pip3 install --user pandas matplotlib   # analyze.sh 画图需要

cd ~
git clone https://github.com/ChatAFLndss/ChatAFL.git
cd ChatAFL
ls -F    # 应看到 aflnet/ ChatAFL/ ChatAFL-CL1/ ChatAFL-CL2/ benchmark/ setup.sh run.sh analyze.sh 等
```

目录说明：

| 目录/文件 | 作用 |
|---|---|
| `ChatAFL/` | 完整版 fuzzer（三大策略全开） |
| `ChatAFL-CL1/` | 只开策略①（消融实验用） |
| `ChatAFL-CL2/` | 只开策略①+②（消融实验用） |
| `aflnet/` | 基线 AFLNet（无 LLM） |
| `benchmark/` | 被测程序 + 结果 |
| `setup.sh` | 构建所有 Docker 镜像 |
| `run.sh` | 跑实验、收集数据 |
| `analyze.sh` | 分析数据、画图 |

---

## 3. 关键改动：DeepSeek 适配（核心）

### 3.1 为什么需要改

ChatAFL 源码把 OpenAI 的**接口地址**和**模型名**硬编码在 `chat-llm.c` 的 `chat_with_llm()` 函数里，并且同时用了两种接口：

| 代码里的 model 参数 | 接口 | 模型 | 用途 |
|---|---|---|---|
| `"turbo"` | `/chat/completions` | `gpt-3.5-turbo` | 语法提取、平台期突破 |
| `"instruct"` | `/completions`（旧版） | `gpt-3.5-turbo-instruct` | 种子扩充 |

DeepSeek 的 API 是 OpenAI 兼容的，但**只有 `/chat/completions` 一种接口**，没有旧版 `/completions`。所以不能"直接换 key"，必须改 4 处。

### 3.2 改了什么（4 处）

文件：`ChatAFL/chat-llm.c`（`ChatAFL-CL1/`、`ChatAFL-CL2/` 同名文件也要改）

1. **URL**：两个分支都改成 `https://api.deepseek.com/chat/completions`
2. **模型名**：`gpt-3.5-turbo-instruct` 和 `gpt-3.5-turbo` → `deepseek-chat`
3. **instruct 请求体**：旧版 `"prompt": "%s"` → chat 版 `"messages": [{"role":"user","content":"%s"}]`
4. **响应解析**：`choices[0].text` → `choices[0].message.content`

### 3.3 一键补丁脚本

`patch_chatafl_deepseek.py`（本仓库根目录）：

```bash
python3 patch_chatafl_deepseek.py ChatAFL/chat-llm.c ChatAFL-CL1/chat-llm.c ChatAFL-CL2/chat-llm.c

# 验证：应无 api.openai / gpt-3.5 残留
grep -n "deepseek\|api.openai\|gpt-3.5" ChatAFL/chat-llm.c
grep -n 'first_choice, "text"' ChatAFL/chat-llm.c      # 应无输出
grep -n 'first_choice, "message"' ChatAFL/chat-llm.c   # 应有 2 行
```

---

## 4. 构建镜像

### 4.1 配置 Docker 代理

```bash
# 1) 守护进程走代理（docker pull 用）
sudo tee -a /etc/default/docker <<'EOF'
export HTTP_PROXY="http://172.29.64.1:7897"
export HTTPS_PROXY="http://172.29.64.1:7897"
export NO_PROXY="localhost,127.0.0.1"
EOF

# 2) docker build 走代理（BuildKit 构建时 apt/git 用）
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "proxies": {
    "http-proxy": "http://172.29.64.1:7897",
    "https-proxy": "http://172.29.64.1:7897",
    "no-proxy": "localhost,127.0.0.1"
  }
}
EOF

sudo service docker restart
sudo docker pull hello-world    # 验证能拉镜像
```

### 4.2 ⚠️ 必须装 buildx（关键踩坑）

`setup.sh` 构建镜像时，被测程序的 Dockerfile 里会 `git clone https://github.com/...` 拉源码。**旧版 builder（legacy）不走 daemon.json 的代理**，导致 `pure-ftpd`、`lightftp`、`forked-daapd` 三个 subject 构建失败（连不上 GitHub）。

解决：装 buildx 启用 BuildKit（BuildKit 会读取 daemon.json 的代理）：

```bash
sudo apt install -y docker-buildx    # 注意：Ubuntu 22.04 上包名是 docker-buildx，不是 docker-buildx-plugin
sudo service docker restart
```

### 4.3 设置 key 并构建

```bash
cd ~/ChatAFL
export KEY=<你的DeepSeek_API_Key>    # sk- 开头
./setup.sh                            # 约 40 分钟，VPN 保持开
```

`setup.sh` 做了三件事：① 用 sed 把 key 写进 `chat-llm.h`；② 把 4 个 fuzzer 源码拷进每个 subject 目录；③ 逐个构建 subject 镜像。

验证镜像是否齐全（官方复现只需这 5 个）：

```bash
for img in kamailio pure-ftpd live555 proftpd exim; do
  docker image inspect $img:latest >/dev/null 2>&1 && echo "✅ $img" || echo "❌ $img"
done
```

---

## 5. 运行实验

### 5.1 冒烟测试（先验证能跑通）

```bash
./run.sh 1 5 pure-ftpd chatafl    # 1 个容器，5 分钟
```

正常结束打印 `CHATAFL: I am done!`。验证 DeepSeek 真的被调用了：

```bash
cd benchmark/results-pure-ftpd
tar -xzf out-pure-ftpd-chatafl_1.tar.gz
ls out-pure-ftpd-chatafl/protocol-grammars/   # 有 llm-grammar-output-* 即成功
```

### 5.2 正式复现（对比 ChatAFL vs AFLNet）

`run.sh` 参数：`./run.sh <容器数> <分钟数> <被测程序列表> <fuzzer列表>`

```bash
./run.sh 1 30 kamailio,pure-ftpd,live555 chatafl,aflnet
#   ↑ 2 fuzzer × 3 subject = 6 个容器并行，各 30 分钟
```

> 注意：run.sh 用 `&` 把任务放到后台并行跑，所以提示符会先回来、`CHATAFL:/AFLNET:` 消息继续刷——**不是卡住**，`docker ps` 能看到容器还在跑。等 6 个 `I am done!` 都出现才算完。

### 5.3 分析

```bash
./analyze.sh kamailio,pure-ftpd,live555 30
```

生成 `res_<subject>_<时间戳>/` 文件夹，含：
- `cov_over_time_<subject>.png` —— 分支覆盖曲线
- `state_over_time_<subject>.png` —— 状态覆盖曲线
- `results-<subject>/results.csv` —— 分支/行覆盖数据
- `results-<subject>/states.csv` —— 状态/转移数据

---

## 6. 结果（30 分钟 × 1 次重复）

### 6.1 状态覆盖（states.csv，最终值）

| 被测程序 | 指标 | AFLNet | ChatAFL | 结果 |
|---|---|---|---|---|
| pure-ftpd (FTP) | 状态数 | 19 | **22** | ✅ ChatAFL |
| | 状态转移 | 89 | **125** | ✅ ChatAFL |
| live555 (RTSP) | 状态数 | 10 | **13** | ✅ ChatAFL |
| | 状态转移 | **81** | 67 | ❌ AFLNet |
| kamailio (SIP) | 状态数 | **10** | 9 | ❌ AFLNet |
| | 状态转移 | **35** | 29 | ❌ AFLNet |

分支覆盖（results.csv）：pure-ftpd 上 ChatAFL ≥690 分支（15%） vs AFLNet 587（12.5%）→ ✅ ChatAFL。

### 6.2 重要观察

**ChatAFL 有"启动开销"**：它要先调 DeepSeek 提取语法 + 扩充种子，所以比 AFLNet **晚 6~25 分钟才开始真正模糊测试**（live555 晚了约 25 分钟，实际只 fuzz 了约 5 分钟），却在 live555 上仍覆盖了更多状态（13 vs 10）——这正体现了论文"更快达到覆盖"的核心结论。

### 6.3 为什么和论文数字不一致（诚实说明）

1. **时间太短**：论文是 24 小时 × 10 次重复；本次是 30 分钟 × 1 次，噪声大。
2. **模型不同**：用 DeepSeek 替代了 GPT-3.5，prompt 是围绕 GPT-3.5 调的，质量有差异。
3. **单次无平均**：论文每个数字是 10 次取平均。

> 要更接近论文，用 `./run.sh 5 240 ...`（5 次 × 4 小时）+ 更强机器。

---

## 7. 理解 LLM 三大策略（产物文件对照）

解压结果后（`out-<subject>-chatafl/`），按执行顺序看这些文件：

| 顺序 | ChatAFL 在做什么 | 文件 | LLM 参与 |
|---|---|---|---|
| 启动① | 调 DeepSeek 提取语法 | `protocol-grammars/llm-grammar-output-0~4` | ✅ |
| 启动② | 语法编译成正则 | `protocol-grammars/pattern-*` | ❌ |
| 启动③ | 调 DeepSeek 扩充种子 | `queue/id:*,orig:enriched_*` | ✅ |
| 运行 | 变异+发消息+存种子 | `queue/` 普通种子 | ❌ |
| 运行（卡壳） | 调 DeepSeek 要下一条消息 | `stall-interactions/request-*` / `response-*` | ✅ |
| 结束 | 记录崩溃 | `replayable-crashes/` | ❌ |

关键文件示例：

- **语法提取**：`cat protocol-grammars/llm-grammar-output-0` → DeepSeek 返回的协议语法（如 `USER: ["USER <<VALUE>>\r\n"]`）
- **种子扩充**：`cat "queue/id:000000,orig:enriched_0_rtsp_requests_aac.raw"` → 可看到 DeepSeek 在 RTSP 序列里**新插入了 `GET_PARAMETER` 和 `TEARDOWN`**（位置正确、Session ID 正确延续）
- **结构感知变异**：`cat protocol-grammars/pattern-0` → `^(?:RETR (.*)\r\n)`，正则里的 `(.*)` 就是"可变区域"，变异只改这里

---

## 8. 踩坑汇总

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 1 | `wsl --install` 403/超时 | 从 GitHub 拉发行版列表，国内被墙 | 挂 VPN 或国内镜像下 `.wsl` 文件 `--from-file` 安装 |
| 2 | C 盘空间不足 | WSL 默认装 C 盘 | `--location E:\...` 指定路径 |
| 3 | WSL 里连不上 GitHub | 代理在 Windows 侧，WSL NAT 不共享 localhost | 在 WSL 里配 `https_proxy` 指向 Windows 宿主 IP |
| 4 | `docker ps` 报 permission denied | usermod 后组未生效 | 退出终端重进 / `newgrp docker` |
| 5 | setup.sh 构建时 3 个 subject 失败 | legacy builder 不走代理，`git clone GitHub` 失败 | 装 `docker-buildx` 启用 BuildKit |
| 6 | `run.sh` 后终端"卡住" | 任务用 `&` 后台跑，`CHATAFL:` 消息还在刷 | 不是卡住，`docker ps` 看容器，等 `I am done!` |
| 7 | `docker build | tail` 看起来没反应 | `tail` 会缓冲所有输出 | 去掉 `tail`，或加 `--progress=plain` |
| 8 | push GitHub 报 "Password authentication is not supported" | GitHub 禁用了账号密码推送 | 用 Personal Access Token (PAT) 代替密码 |
| 9 | ⚠️ API key 泄露风险 | `setup.sh` 把 key 写进 `chat-llm.h` | 提交前 `sed` 还原成 `"1"` 占位符，别提交真实 key |

---

## 9. 提交到自己的 GitHub

```bash
cd ~/ChatAFL

# 1. 还原 key（防止泄露）
sed -i 's/#define OPENAI_TOKEN ".*"/#define OPENAI_TOKEN "1"/' ChatAFL/chat-llm.h ChatAFL-CL1/chat-llm.h ChatAFL-CL2/chat-llm.h

# 2. 复制补丁脚本进仓库
cp /mnt/c/.../patch_chatafl_deepseek.py .

# 3. 忽略大产物
cat >> .gitignore <<'EOF'
benchmark/results-*/
res_*/
*.tar.gz
*.orig
benchmark/subjects/*/*/aflnet/
benchmark/subjects/*/*/chatafl/
benchmark/subjects/*/*/chatafl-cl1/
benchmark/subjects/*/*/chatafl-cl2/
EOF

# 4. 配置身份 + 提交
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
git add -A
git commit -m "Adapt ChatAFL to DeepSeek API + reproduction notes"

# 5. 指向自己的仓库并推送
git remote rename origin upstream
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin master    # 密码填 Personal Access Token
```

---

## 10. 附录：完整命令速查

```bash
# ===== 环境 =====
wsl --install --no-distribution && wsl --update
wsl --install Ubuntu-22.04 --location E:\WSL\Ubuntu-22.04

# ===== WSL 内代理 =====
HOST_IP=$(ip route show default | awk '{print $3}')
export http_proxy="http://$HOST_IP:7897" https_proxy="http://$HOST_IP:7897" all_proxy="http://$HOST_IP:7897"

# ===== 依赖 =====
sudo apt update && sudo apt install -y git python3 python3-pip docker.io docker-buildx
sudo usermod -aG docker $USER && sudo service docker start
pip3 install --user pandas matplotlib

# ===== 代码 =====
git clone https://github.com/ChatAFLndss/ChatAFL.git && cd ChatAFL

# ===== DeepSeek 补丁 =====
python3 patch_chatafl_deepseek.py ChatAFL/chat-llm.c ChatAFL-CL1/chat-llm.c ChatAFL-CL2/chat-llm.c

# ===== 构建 =====
export KEY=<DeepSeek_key>
./setup.sh

# ===== 实验 =====
./run.sh 1 30 kamailio,pure-ftpd,live555 chatafl,aflnet
./analyze.sh kamailio,pure-ftpd,live555 30

# ===== 看结果 =====
cd benchmark/results-pure-ftpd && tar -xzf out-pure-ftpd-chatafl_1.tar.gz
cat out-pure-ftpd-chatafl/protocol-grammars/llm-grammar-output-0     # 语法
ls out-pure-ftpd-chatafl/queue/ | grep enriched                        # 扩充种子
ls out-pure-ftpd-chatafl/stall-interactions/                           # 平台期交互
```
