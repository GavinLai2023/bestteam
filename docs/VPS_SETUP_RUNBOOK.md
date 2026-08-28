# VPS 部署 Runbook（从空服务器到能打开的网站）

这份文档补上 `docs/deployment.md` 第 0 节第 1 步那一行字——「Provision a host with
Docker, clone the repo, write `.env`」——背后真正要做的事。`deployment.md` 是从
「你已经有一台配好 Docker 的机器」开始写的；这份文档负责把你送到那个起点，并且
一路配到浏览器里能用 `https://` 打开。

目标读者是**不熟悉 Linux/Docker 运维的人**。每一步都写明「做什么」「为什么」
「怎么确认做对了」「出错了怎么办」。

## 0. 开始之前

**前提**：

- 一台已经开好的 DigitalOcean Droplet（Ubuntu 24.04 LTS），你有 root 登录方式
- 一个已经买好的域名（本文以 `bestteam.online` 为例）
- 一个能改这个域名 DNS 解析的后台账号
- 至少一个大模型的 API Key（OpenAI / Anthropic / 其他）

**做完之后你会得到**：

```
                浏览器
                  │  https://bestteam.online
                  ▼
        ┌───────────────────┐
        │  Caddy（跑在主机上）│  ← 负责 HTTPS 证书，自动申请、自动续期
        └─────────┬─────────┘
                  │
        ┌─────────┴──────────┐
        ▼                    ▼
  /api/* 开头的请求      其余所有请求
        │                    │
        ▼                    ▼
  ┌───────────┐        ┌───────────┐
  │  backend  │        │ frontend  │   ← 两个 Docker 容器
  │  :8000    │        │  :8080    │      只监听 127.0.0.1，外网碰不到
  └───────────┘        └───────────┘
```

**为什么前后端共用一个域名**：只需要一张证书；没有跨域配置；而且分享聊天的访客
身份是绑在域名上的（`SameSite=Lax`），同域名下天然正确——前端和后端如果分在两个
不同的注册域名下，访客每发一句话都会变成一段新对话。

**顺序不能乱的三个地方**（后面每一步会再提醒一次）：

1. **域名解析要在装 Caddy 之前生效** —— Caddy 申请证书时要求域名已经指向这台机器
2. **`.env` 要在 `docker compose build` 之前写好** —— 网址是**编译**进前端的，不是运行时读的
3. **改完 `docker-compose.yml` 再构建** —— 端口绑定要在容器创建时就对

---

## 1. 确认服务器规格

登录服务器（DigitalOcean 控制台 → 你的 Droplet → Console，或者用本机终端
`ssh root@<你的IP>`），先看看这台机器有多大：

```bash
free -h        # 内存
nproc          # CPU 核数
df -h /        # 磁盘
```

**及格线**：内存 4 GB、2 核、磁盘 50 GB。

**为什么**：后端容器本身限制在 2 GB；构建前端时要跑一次 Node 打包，是整个流程里
最吃内存的一步，2 GB 的机器很容易在这一步被系统杀掉（报错信息通常只有一句
`Killed`，很难看出是内存问题）。

**如果内存不到 4 GB**：先加一块交换分区（相当于拿磁盘临时当内存用，慢但够构建
用）：

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

确认：`free -h` 的 `Swap` 那一行不再是 0。

> 加了 swap 能让你把系统跑起来，但长期不建议——机器忙起来会明显变卡。如果打算正
> 式接客户，把 Droplet 升到 4 GB（DigitalOcean 后台 Resize 即可，需要关机几分钟）。

---

## 2. 基本安全设置

**做什么**：建一个日常用的账号（不要一直用 root），打开防火墙。

```bash
# 建账号，<你的用户名> 换成你想要的，比如 gavin
adduser <你的用户名>              # 会提示设密码，其余问题一路回车
usermod -aG sudo <你的用户名>     # 让它能用 sudo
```

把 root 的 SSH 登录方式复制给新账号，否则新账号登不上：

```bash
rsync --archive --chown=<你的用户名>:<你的用户名> ~/.ssh /home/<你的用户名>
```

打开防火墙，只放行三个端口：

```bash
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw enable          # 会问 y/n，输入 y
ufw status          # 确认三条规则都在
```

**为什么只开这三个**：22 是你自己登录用的，80 和 443 是网站用的（80 用于自动跳转
到 HTTPS 和申请证书）。后端的 8000 端口**不需要**对外开放——第 6 步会让它只监听
本机。

**怎么确认做对了**：**另开一个终端窗口**，用新账号登录一次：

```bash
ssh <你的用户名>@<你的IP>
```

登录成功之后，才关掉原来那个 root 窗口。

> **出错了怎么办**：如果新账号登不上，别关 root 那个窗口——你还有救。回到 root 窗
> 口检查 `ls -la /home/<你的用户名>/.ssh`，看 `authorized_keys` 在不在、属主对不对。
> 万一两个窗口都关了又登不上，DigitalOcean 控制台的 **Console** 是不走 SSH 的后门，
> 一定能进去。

**后面所有步骤都用新账号操作。**

---

## 3. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

第二条命令是让你不用每次都打 `sudo docker`。它要**重新登录**才生效：

```bash
exit
# 然后重新 ssh 进来
```

**怎么确认做对了**：

```bash
docker run --rm hello-world
```

看到一段 `Hello from Docker!` 就对了。如果提示 `permission denied`，说明上面那次重
新登录没生效，再退出重进一次。

---

## 4. 把代码放到服务器上

代码在私有仓库里，所以服务器需要一把**只读的钥匙**才能拉取。

**第一步，在服务器上生成一把钥匙**：

```bash
ssh-keygen -t ed25519 -C "bestteam-vps" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

最后一条命令会打印一行 `ssh-ed25519 AAAA...` 开头的文字，**整行复制下来**。

**第二步，把它加到 GitHub**：

打开 `https://github.com/GavinLai2023/bestteam` → **Settings** → 左侧
**Deploy keys** → **Add deploy key**：

- Title：随便写，比如 `vps-production`
- Key：粘贴刚才复制的那一行
- **Allow write access：不要勾**（服务器只需要读，不需要往回推代码）

**为什么用 Deploy key 而不是你自己的账号密码**：这把钥匙只能读这一个仓库，而且随
时可以在同一个页面删掉。万一服务器出事，影响面到此为止。

**第三步，把代码拉下来**：

```bash
sudo mkdir -p /opt/bestteam
sudo chown $USER:$USER /opt/bestteam
git clone git@github.com:GavinLai2023/bestteam.git /opt/bestteam
cd /opt/bestteam
```

第一次连接会问 `Are you sure you want to continue connecting?`，输入 `yes`。

**怎么确认做对了**：`ls` 能看到 `docker-compose.yml`、`Dockerfile`、`docs/` 这些。

> **后面所有命令都在 `/opt/bestteam` 目录下执行**。如果你重新登录了，先
> `cd /opt/bestteam`。

---

## 5. 配置域名解析

**这一步必须在装 Caddy 之前完成并生效**，因为 Caddy 申请证书时，证书机构会反过来
访问 `bestteam.online` 验证这台机器真的是你的。域名还没指过来，申请就会失败。

到你买域名的后台，添加一条 **A 记录**：

| 类型 | 主机记录 | 值 |
|------|----------|-----|
| A | `@`（表示 `bestteam.online` 本身） | 你的 Droplet IP |

**怎么确认做对了**：在服务器上执行

```bash
dig +short bestteam.online
```

打印出来的应该正好是你的 Droplet IP。

> **出错了怎么办**：DNS 生效需要时间，可能几分钟也可能几小时（取决于域名商）。
> 如果打印的是空的或者是旧地址，**先等着，别往下走**。每隔几分钟再跑一次这条命令。
> 装 Caddy 之前它必须是对的。

---

## 6. 修改两处端口绑定

用编辑器打开 `docker-compose.yml`：

```bash
nano docker-compose.yml
```

> nano 的用法：方向键移动，直接打字修改，`Ctrl+O` 然后回车保存，`Ctrl+X` 退出。

找到 **backend** 下面的：

```yaml
    ports:
      - "8000:8000"
```

改成：

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

找到 **frontend** 下面的：

```yaml
    ports:
      - "80:80"
```

改成：

```yaml
    ports:
      - "127.0.0.1:8080:80"
```

**为什么必须改这两行，有两个独立的理由**：

1. **安全**。这是最重要的一条：**Docker 发布的端口会绕过 ufw 防火墙**。也就是说，
   即使你在第 2 步没有放行 8000，`"8000:8000"` 这种写法**依然会让全世界都能直接访
   问你的后端**，绕过 HTTPS、绕过 Caddy。加上 `127.0.0.1:` 前缀，端口就只对本机开
   放，外网完全碰不到——这才是真正拦住它的东西，不是防火墙规则。
2. **腾出 80 端口**。前端容器原本占着 80，而 Caddy 需要 80 和 443。把前端挪到 8080
   （只对本机），80 就空出来给 Caddy 了。

**升级代码时要注意**：这是你对仓库文件做的本地修改，将来 `git pull` 会冲突。标准
做法是：

```bash
git stash          # 先把本地修改收起来
git pull           # 拉新代码
git stash pop      # 把修改放回去
```

---

## 7. 写 `.env` 配置文件

```bash
cp .env.example .env
nano .env
```

下面是**你这套部署需要改的每一项**，其余的保持原样（空着就是关闭）。

### 7.1 两把密钥（先生成，再填）

在服务器上执行这两条命令，各得到一串随机字符：

```bash
# 第一把：签名登录凭证用
openssl rand -hex 32

# 第二把：加密客户邮箱密码用
openssl rand -base64 32 | tr '+/' '-_'
```

分别填进去：

```
BESTTEAM_SECRET_KEY=<第一条命令的输出>
BESTTEAM_SECRETS_KEY=<第二条命令的输出>
```

**这两把必须不一样**，系统会检查。一把泄露不至于让另一把也失守。

> ⚠️ **`BESTTEAM_SECRETS_KEY` 要单独备份到密码管理器里，而且绝对不要和数据库备份
> 放在同一个地方。** 所有客户的邮箱密码都是用它加密存在数据库里的——把钥匙和保险
> 箱放一起，等于没锁。这把钥匙丢了，后端会拒绝启动，客户的邮箱要一个一个重新连。

### 7.2 网址（三处，必须在构建之前填对）

```
BESTTEAM_CORS_ORIGINS=https://bestteam.online
VITE_API_BASE=https://bestteam.online
VITE_WS_BASE=wss://bestteam.online
```

注意 `VITE_WS_BASE` 开头是 **`wss`** 不是 `https`（这是实时推送用的协议）。三个都
**不要带结尾的斜杠**。

> **为什么强调「在构建之前」**：`VITE_` 开头的两个值是**编译**进前端文件里的，不是
> 运行时读的。填错了改配置没用，必须重新 `docker compose build`。

### 7.3 反向代理

```
FORWARDED_ALLOW_IPS=*
```

**为什么可以填 `*`**：这个值告诉后端「可以相信谁转发过来的访客真实 IP」。因为第 6
步已经把后端绑在 `127.0.0.1` 上，能连到它的**只有 Caddy 一个**，所以无条件相信是
安全的。不填的话，所有登录尝试在后端看来都来自同一个地址，登录失败次数的限制会算
到所有人头上。

### 7.4 大模型 API Key

按你实际用的填，至少要有一个：

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

### 7.5 留存期（建议现在就设）

```
BESTTEAM_RUN_RETENTION_DAYS=90
```

**为什么现在设**：这个值只对**之后新建**的客户组织生效，已经建好的不会追溯。留着不
设，客户的邮件处理记录就永久保存——那里面有他们客户的姓名和邮件内容。

### 7.6 知识库检索质量（不设的话客户只有关键词检索）

```
BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL=openai:text-embedding-3-small
BESTTEAM_KB_DEFAULT_RERANK_MODEL=cross-encoder:BAAI/bge-reranker-base
```

**为什么必须现在设**：这两项留空，系统照样启动、知识库照样能用，所以很容易漏掉。
但客户拿到的是纯关键词匹配——**客户换个说法提问，或者用中文问一份英文文档，检索结
果是空的**（实测 recall 为 0.00）。而且建团队向导里的「Enhanced（智能检索）」开关
只有设了第一项才会出现，客户根本看不到这个选择。

第二项是本地跑的重排模型，不花钱、不调用外部服务，但第一次启动会下载约 1.1 GB 模
型文件。这个具体型号是发布门槛实测选出来的（换成其他多语言模型会把中文译文排在正
确的英文原文前面）。

> 第一项走 OpenAI，会产生费用（很低，按嵌入 token 计），用的是 7.4 里那个
> `OPENAI_API_KEY`。**不要填 `fake:` 开头的值**，那是测试用的假模型，检索结果是噪
> 声——`check-env` 会直接报 `[FAIL]`。

### 7.7 确认这几项是空的

```
BESTTEAM_DEMO_PIPELINES=
BESTTEAM_EMAIL_BACKEND=
BESTTEAM_IMAP_HOST=
BESTTEAM_GRAPH_TENANT_ID=
```

- `BESTTEAM_DEMO_PIPELINES` 一旦打开，**每个客户都能看到并运行我们的演示团队**
- `BESTTEAM_EMAIL_*` 是「整个系统共用一个邮箱」的老路子。多客户部署下后端会**直接
  拒绝启动**。客户邮箱要一个一个单独连（`deployment.md` §4c）

### 7.8 可选：错误上报

```
BESTTEAM_SENTRY_DSN=
```

留空也能跑，只是出问题时你只能翻服务器日志。想要出错时有个地方能看，去
[sentry.io](https://sentry.io) 注册（免费额度对 Beta 足够），把 DSN 填进来。
**填错格式会导致后端起不来**，所以要么正确填写，要么留空。

### 7.9 可选：邮件轮询间隔 / 每日运行上限

```
BESTTEAM_TRIGGER_POLL_SECONDS=120
BESTTEAM_TRIGGER_DAILY_CAP=50
```

两个都留空的话默认就是这两个值，都是**部署级**、对全平台所有客户统一生效，界面上
客户或组织管理员都改不了。

- `BESTTEAM_TRIGGER_POLL_SECONDS`：多久去看一眼邮箱一次，默认 120 秒。这一步只是
  查收件箱有没有新邮件（IMAP/Graph API 调用），不涉及大模型，不花 token。调小则发
  现新邮件更快，但对邮件服务商的请求更频繁；调大则相反。
- `BESTTEAM_TRIGGER_DAILY_CAP`：每个邮件触发器一天最多能自动运行几次流水线，默认
  50。真正调用大模型、花 token 的是这一步，见下方说明。到达上限后触发器当天不再
  自动运行，要等第二天（`runs_today`/`runs_date` 在下一个自然日自动清零）。

它和客户在「邮件预算」页自己设的每日消息数/每月花费上限是两回事、互不替代：客户
的预算是每个组织自己设、可以更严格；这个值是整个平台统一的硬上限，只有改这个环境
变量才能调整。默认的 50 对邮件量大的客户可能偏低，按需调大。

---

## 8. 构建并启动

```bash
cd /opt/bestteam
docker compose build
```

**第一次构建要 5–15 分钟**，屏幕会滚很多行，正常。

```bash
docker compose up -d
```

**怎么确认做对了**：

```bash
docker compose ps
```

要看到两个容器，backend 的状态是 **`healthy`**（不是只有 `Up`）。健康检查大约需要
30 秒才第一次出结果，刚起来时显示 `starting` 是正常的，等一下再看。

```bash
curl http://127.0.0.1:8000/api/health
```

期望输出：`{"status":"ok","database":"ok"}`

再跑一次启动检查：

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
```

**期望：所有行都是 `[OK]` 或 `[WARN]`，一个 `[FAIL]` 都没有。**

`[WARN]` 可以先放着（多半是「你还没连邮箱」「你没配错误上报」这类），`[FAIL]` 必
须改完 `.env` 再 `docker compose up -d` 一次，直到全部通过。

> **出错了怎么办**：容器起不来先看日志——
> `docker compose logs --tail 100 backend`。后端在配置不对时会明确说出是哪一项，
> 不用猜。改完 `.env` 之后：
> - 只改了非 `VITE_` 的项 → `docker compose up -d` 就够了
> - 改了 `VITE_API_BASE` / `VITE_WS_BASE` → 必须
>   `docker compose build frontend && docker compose up -d`

---

## 9. 配置 HTTPS

**开始之前再确认一次**：`dig +short bestteam.online` 打印的是这台机器的 IP。不对就
回第 5 步等着。

**安装 Caddy**：

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

**为什么用 Caddy**：它会自动申请 HTTPS 证书、自动续期，配置只有几行。其他方案要你
自己跑证书申请命令、自己配定时续期，多好几个会出错的环节。

**写配置**：

```bash
sudo nano /etc/caddy/Caddyfile
```

**把文件里原有的内容全部删掉**，换成：

```
bestteam.online {
	# 上传的知识库文档可能很大，放宽请求体上限
	request_body {
		max_size 250MB
	}

	# /api/ 开头的都交给后端，包括实时推送
	handle /api/* {
		reverse_proxy 127.0.0.1:8000
	}

	# 其余的都是网页本身
	handle {
		reverse_proxy 127.0.0.1:8080
	}
}
```

> 注意：Caddyfile 的缩进要用 **Tab**，不要用空格。

**生效**：

```bash
sudo systemctl reload caddy
```

第一次生效时 Caddy 会去申请证书，需要几秒到一分钟。

**怎么确认做对了**：

```bash
curl https://bestteam.online/api/health
```

期望：`{"status":"ok","database":"ok"}`。能看到这个，就说明**域名、证书、反向代理、
后端**这一整条链路全通了。

> **出错了怎么办**：`sudo journalctl -u caddy --no-pager -n 50` 看 Caddy 的日志。
> 最常见的两种：
> - **证书申请失败** → 十有八九是域名还没解析过来，或者防火墙没放行 80 端口。回第
>   5 步和第 2 步各确认一次。
> - **502 Bad Gateway** → Caddy 通了但后面的容器没起来。`docker compose ps` 看看。

---

## 10. 验收清单

全部打勾才算这份文档做完了：

- [ ] `docker compose ps` → 两个容器都在，backend 是 `healthy`
- [ ] `check-env` → 没有 `[FAIL]`
- [ ] `check-env` 里 `BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL` 那行是 `[OK]`，不是 `[WARN]`（`[WARN]` = 客户只有关键词检索，见 7.6）
- [ ] `curl https://bestteam.online/api/health` → `{"status":"ok","database":"ok"}`
- [ ] 浏览器打开 `https://bestteam.online` → 看到登录页，地址栏有**小锁**、没有「不安全」警告
- [ ] `curl http://<你的IP>:8000/api/health` **从你自己的电脑上跑** → 应该**连不上**（后端没有暴露在公网，这是对的）

最后一条是安全验收，别跳过。如果它居然通了，说明第 6 步的端口绑定没生效——回去检
查 `docker-compose.yml`，改完执行 `docker compose up -d`。

---

## 11. 日常运维小抄

```bash
cd /opt/bestteam

# 看后端日志（Ctrl+C 退出）
docker compose logs -f backend
docker compose logs --tail 200 backend      # 只看最近 200 行

# 重启
docker compose restart

# 升级到新版本代码
git stash && git pull && git stash pop      # 保住你改过的端口绑定
docker compose build
docker compose up -d
# 数据库结构的升级是容器自己做的，不用手动执行

# 看服务器还剩多少资源
free -h && df -h /
```

**换域名的时候要做什么**（将来从临时域名换到正式域名）：

1. 新域名加 A 记录指向这台机器，`dig +short <新域名>` 确认生效
2. `.env` 里三处改成新域名（`BESTTEAM_CORS_ORIGINS`、`VITE_API_BASE`、`VITE_WS_BASE`）
3. `/etc/caddy/Caddyfile` 第一行的域名改掉，`sudo systemctl reload caddy`
4. `docker compose build frontend && docker compose up -d`

数据、客户配置、已连接的邮箱**都不受影响**。唯一会坏的是**已经发出去的分享链接**
——如果那时候已经有链接在客户手里，旧域名先别退，留着做跳转。

---

## 12. 接下来

到这里你有了一台能用的部署，但**还没有任何真实客户数据**。下一步是
`docs/PRELAUNCH_DRILLS_RUNBOOK.md`——在真接客户之前，把「崩溃能不能自己恢复」「备
份能不能真的恢复回来」「微软 365 邮箱能不能连上」这三件事各演练一遍。

那份文档的第 1 节会让你确认基础环境就绪（就是你刚做完的这些），然后建一个叫
`drilltest` 的临时组织开始演练。演练完清掉临时数据，再按 `docs/deployment.md` 第 0
节的清单接第一个真客户。

**不要跳过演练直接接客户**：一个从来没有真正跑过的恢复脚本，不算备份方案。
