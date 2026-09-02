# 部署 bestteam（中文版，写给不熟悉运维的人）

> 这是 `docs/deployment.md`（英文原版）的中文说明，内容上尽量对应，但写得更啰嗦
> 一些——每一步除了「做什么」，也会说「为什么」和「怎么确认自己做对了」。英文原
> 版是最新、最权威的那一份；这份中文版靠人工跟着改，如果两边看起来对不上，以英
> 文原版为准，或者提醒维护者更新这一份。
>
> 如果你还没有一台配好 Docker 的服务器，先看 `docs/VPS_SETUP_RUNBOOK.md`——那份
> 文档专门讲怎么从一台空服务器，一路配到能用 `https://` 打开网站，本文档是接着
> 它往下讲的。

## 这套系统是怎么"部署"的

bestteam 可以给**一个客户单独部署一套**，也可以**一套系统装很多个客户**（专业说
法叫"多租户"）。代码是同一份，区别只在于你给它建了几个"组织"（org）——建一个组
织，就是单客户模式；建多个，每个客户一个组织，就是多客户模式。

不管哪种模式，一次部署跑的都是两个程序：

- **backend**（后端）：处理业务逻辑、存数据，数据存在一个叫 SQLite 的文件数据
  库里（不需要单独装数据库软件，就是一个文件）。
- **frontend**（前端）：用户在浏览器里看到、点击的界面。

这两个程序都装在 Docker 容器里，用 Docker Compose 统一启动。如果你对 Docker 完
全陌生：可以把它理解成"把程序和它需要的所有依赖打包成一个盒子，在任何机器上都
能原样跑起来"，`docker compose up` 就是"把这些盒子都启动"。

> **多客户模式下要注意的一件事**：如果你在 `.env` 里配置了
> `BESTTEAM_EMAIL_*` 这几个环境变量，它们只能代表**一个**邮箱，是给整个进程用
> 的。所以只要你打算装第二个客户，后端会直接拒绝启动（`create-org` 建第二个组
> 织也会被拒绝）。多客户场景下，每个客户的邮箱要用命令行工具单独连（下面的 §4c
> 会讲），不要用这几个环境变量。这条规则不止管邮箱——任何"整个进程只能配一份"的
> 集成设置，在多客户模式下都要小心。

## 第一次上线，照这个顺序做

下面每一步都有自己的章节讲细节；这里先给一张顺序表，因为顺序这件事只有三处真
的不能乱（打勾的地方后面会再提醒一次）：

| 步骤 | 做什么 | 在哪一节 |
|---|------|------|
| 1 | 准备一台装好 Docker 的服务器，把代码 clone 下来，写好 `.env` 配置文件 | `docs/VPS_SETUP_RUNBOOK.md`（从零开始，一步步教），然后看本文 §1 |
| 2 | **先搞清楚客户用的是什么邮箱**——不要直接问客户 | §0a |
| 3 | 对着真实环境跑一遍上线前检查清单 | §1「上线前检查清单」 |
| 4 | `docker compose build && docker compose up -d`（构建并启动） | §2 |
| 5 | `alembic upgrade head`（把数据库结构建好） | §3 |
| 6 | `create-org` 建一个组织（如果只服务一个客户，可以跳过——系统自带一个叫
      "default"的组织） | §4 |
| 7 | `create-user` 给客户建一个账号 | §4 |
| 8 | 给自己建一个平台管理员账号（`create-user --platform` + `promote`） | §4b |
| 9 | `set-email <组织> ... --test` 接上客户的邮箱 | §4c |
| 10 | **如果邮箱是 Microsoft 365 的**，对着真实租户走一遍
       `docs/email-smoke-test.md` 第 9 节，最好客户在场 | §4c |
| 11 | 如果客户希望数据不要无限期保留，设置一个保留天数 | §4「保留和清理运行记录」 |
| 12 | 装上每晚自动备份的定时任务，并把备份文件另外拷一份出去 | 「备份与恢复」 |
| 13 | 把 `BESTTEAM_SECRETS_KEY` 存进密码管理器，**不要**和备份文件放在一起 | 「备份与恢复」 |
| 14 | 客户正式用之前，先在一套"用完就扔"的环境上演练一次
       `scripts/restore.sh` 恢复流程 | 「备份与恢复」 |
| 15 | 把 `docs/BETA_NOTES.md` 交给客户看 | — |

第 12～14 步是最容易在赶时间的时候被跳过的，也恰恰是跳过之后代价最大的三步。一
个从来没跑过的恢复脚本，不能算是"有备份"。

### 0a. 客户的邮箱是哪种？

**不要直接问客户"你们用的是 Microsoft 365 还是 IMAP？"**——这个问题对一个不懂技
术的人来说根本答不出来，答错了还可能让客户的 IT 部门去配错东西。正确做法是从
邮箱地址本身去判断：

| 邮箱地址 | 结论 |
|---------|------|
| `@outlook.com`、`@hotmail.com`、`@live.*`、`@msn.com` | **个人版 Microsoft 账号，不支持。** 微软已经关闭了这类账号的传统密码登录方式，而另一条路（OAuth 授权）需要一个企业级的 Entra 租户，个人账号没有这个东西。客户需要换一个工作邮箱。 |
| `@gmail.com` | 用 IMAP 加**应用专用密码**（不是账号本身的登录密码）；客户要先开启两步验证，才能生成这种密码。 |
| `@qq.com`、`@163.com`、`@126.com` 等国内邮箱 | 用 IMAP，配合邮箱服务商发的**授权码**（不是登录密码）。客户要先在邮箱设置里把 IMAP 功能打开。 |
| 公司自己的域名（比如 `@客户公司.com`） | 去查这个域名的 MX 记录（见下面）。 |

如果是公司自己的域名，一条命令就能查出结果：

```bash
# Linux / macOS
dig +short MX example.com
# Windows
nslookup -type=mx example.com
```

- 查出来的结果指向 **`*.mail.protection.outlook.com`** → 说明用的是
  **Microsoft 365**。要用 `--auth microsoft-oauth`（见 §4c），而且客户的 IT
  部门需要在他们自己的后台注册一个应用、同意 `IMAP.AccessAsApp` 权限、并且把
  这个应用的权限限定到那一个邮箱上——具体 PowerShell 命令在 §4c 的"Microsoft
  365 邮箱"部分。**这一步要预留足够的时间**：这是要改客户自己租户的配置，操
  作的人不在你的会议室里，需要来回沟通。
- 查出来的结果指向 **`*.google.com` / `*.googlemail.com`** → 说明用的是
  Google Workspace（企业版 Gmail）。跟普通 Gmail 一样，用 IMAP + 应用专用密码。
- 其他情况 → 普通 IMAP。找客户的 IT 要 IMAP 服务器地址、用户名，以及要不要用
  应用专用密码。下面提到的 `--test` 参数（§4c）会在真正保存之前先试着登录一
  次，所以答案填错了顶多重新敲一条命令，不会拖成一次支持工单。

不管是哪种情况，一个客户从头到尾只需要一个邮箱、一个组织、一个自动收发邮件的
AI 团队（详见 `docs/BETA_NOTES.md`）。

**三份可以直接发给客户的连接指引。**发链接，不要发拷贝：每份都有中英两版，
中文在 `/zh/setup/...`，英文在 `/setup/...`，页面自带的语言链接可以互跳。

- <https://bestteam.online/zh/setup/gmail> —— 客户自己十分钟就能做完。
- <https://bestteam.online/zh/setup/m365> —— **这条路客户做不了。**它需要客户
  自己租户里的全局管理员，其中一步只能在 PowerShell 里完成，所以这份文档是按
  "你和客户共享屏幕、照着念"的脚本写的。**提前发过去**，让他们先去找出谁手里
  有管理员账号，拖天数的是这件事，不是操作本身。
- <https://bestteam.online/zh/setup/m365-it> —— 同样的配置，给**有 IT 的客户**，
  或者租户握在代理商手里的情况：转发过去他们自己就能做完。开头是权限规格表，
  把"这是不是 basic auth"、"为什么要 FullAccess"这两个必被问到的问题提前答掉，
  另有出网地址、审计、密钥轮换与吊销。**每个客户只发两份 M365 文档中的一份**：
  给 IT 发共享屏幕脚本是浪费对方时间，给非技术老板发规格表则第一张表就把人劝
  退了。

这三页在 `bestteam-website` 仓库的 `src/pages/setup/` 下，不在本仓库。每份只有
一处来源，向导的邮箱步骤一改，不会留下一份和屏幕对不上的旧说明。

## 1. 配置环境变量

先把示例配置文件复制一份出来：

```bash
cp .env.example .env
```

这一步只需要做一次：`.env` 已经在 `.gitignore` 里，以后拉取新代码或者重新构
建都不会碰它——**不要**在已经部署好的机器上重复执行这条 `cp`，那样会用模板文
件把你配置好的 `.env` 整个覆盖掉。至于"这次升级有没有要求填新的环境变量"，那
是升级流程里的事，而且必须等新代码真的拉到服务器上之后才能查——见后面「升级一
个已有的部署」一节。

然后打开 `.env`，把下面这些填好：

- 大模型服务商的密钥（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`，如果要用网络搜索
  还要填 `TAVILY_API_KEY`）。
- `BESTTEAM_SECRET_KEY`——这是给系统内部用来签名/加密会话的密钥。**如果这一项
  还是示例文件里的默认值，后端会直接拒绝启动**（不管在什么环境下）。用下面这
  条命令生成一个真正随机的值：
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- `BESTTEAM_CORS_ORIGINS`——填客户前端网页的地址（可以填多个，用逗号分开），
  末尾不要带斜杠 `/`。这一项管的是"哪些网址允许调用这个后端的接口"。
- `VITE_API_BASE` / `VITE_WS_BASE`——后端对外能被访问到的地址（分别是
  `https://...` 和 `wss://...` 开头）。**注意：这两项是在"构建"前端的时候就写
  死进去的**，不是运行时读取的，所以改了这两项之后要重新构建前端（见 §2）。
- `BESTTEAM_SECRETS_KEY`——这是另外一把加密密钥，专门用来加密"存在数据库里的敏
  感信息"（目前只有各个客户的邮箱密码，不管是你用命令行帮客户连的，还是客户自
  己在界面里连的）。**整个部署只用一把钥匙，不是一个客户配一把**：每个客户的
  邮箱密码都是用这同一把钥匙加密的，这把钥匙是你（运维的人）在搭建服务器的时
  候生成、设置**一次**的。客户永远看不到它，也不需要提供它——它跟客户没有任何
  关系。一旦有客户连了邮箱，这一项就是必须的了：如果后端解不开存好的密码，会
  直接拒绝启动。**这把钥匙必须和上面的 `BESTTEAM_SECRET_KEY` 不一样**，而且要
  存在环境变量或者专门的密钥管理工具里，**绝对不能存进数据库**。原因很直接：
  如果把解密用的钥匙和被它加密的密码存在同一个地方，那加密就等于白做了——谁拿
  到了数据库的备份文件，谁就同时拿到了"锁起来的密码"和"打开锁的钥匙"。生成方
  式：
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `BESTTEAM_DEMO_PIPELINES`——**给正式客户部署的时候，这一项不要设置。** 设置
  了之后，`ui/backend/pipelines/` 目录下自带的几个演示用 AI 团队会对所有人可
  见、可运行——这些团队不属于任何客户组织，所以每一个登录用户都能看到、能跑。
  这些演示团队大多数返回的是写死的假文本，看起来像真的回答；其中
  `email_triage_demo_live` 这一个是真的会去读 `BESTTEAM_EMAIL_*` 那个环境变量
  指定的邮箱。只在开发环境或者给销售演示用的环境上，才把这一项设成 `1`。

HTTPS（浏览器地址栏那把小锁）不是由这两个容器自己处理的，而是交给前面挡着的反
向代理（比如 Caddy、Nginx）或者云平台自带的负载均衡器来做。`docs/VPS_SETUP_
RUNBOOK.md` 里配的就是 Caddy。

**登录限流和"客户端地址"的关系。** `/api/auth/login`（登录接口）会限制失败次数：
同一个用户名 15 分钟内最多失败 5 次，同一个来源地址 15 分钟内最多失败 20 次，
超过之后会返回 429 状态码并附带一个"多久之后再试"的提示。这里有个容易踩的坑：
Uvicorn（跑后端的那个程序）只有在**信任**某个反向代理的情况下，才会去读这个代
理转发过来的 `X-Forwarded-For` 头（也就是"真实访客地址"）。所以你在自己的反向
代理后面部署时，一定要在 `.env` 里把 `FORWARDED_ALLOW_IPS` 设成这个代理自己的
地址（如果后端端口只有代理能访问到，也可以直接设成 `*`）——不然的话，所有登录
请求在后端看来都是从"代理"这一个地址发来的，导致这个"按地址限流"的额度被所有
用户共用，一个人连续登录失败几次，可能就把别的用户也一起限制住了。按用户名限
流的那条规则不受这个问题影响。

### 上线前检查清单

在给一个正式客户第一次 `docker compose up -d` 之前，对着**真实的**运行环境跑
一遍这个检查：

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
```

它只是读配置、打印结果，不会碰数据库——在还没建数据库的机器上跑，跑完也还是没
有数据库。每一项配置打印一行结果，只要有一项标记为 `[FAIL]`，这条命令本身就会
以失败状态退出（方便接到自动化脚本里）。它检查的内容，以及为什么要检查：

| 检查项 | 级别 | 为什么要查 |
|------|-------|-----------------------|
| `BESTTEAM_SECRET_KEY` 已设置，且不是示例值 | FAIL（必须改） | 不改的话后端直接拒绝启动；在这里先发现，总比线上不停重启崩溃了才发现好。 |
| `BESTTEAM_SECRETS_KEY` 已设置，是一个合法的密钥，而且**跟上面那把不一样** | 没设：WARN（提醒）；跟上面那把一样或格式不对：FAIL | 一旦有客户连了邮箱就必须有这一项；如果两把钥匙用成同一把，一把泄露就等于两把都泄露。 |
| `BESTTEAM_CORS_ORIGINS` 填的是具体地址，没有用 `*` 通配符，末尾没有斜杠 | FAIL | 用通配符会被系统直接拒绝；地址填错的话，前端页面完全没办法调用后端接口。 |
| `VITE_API_BASE` / `VITE_WS_BASE` 已设置，分别是 `https://` / `wss://` 开头 | 没设：FAIL | 这两项是构建前端镜像时写死进去的——填错了意味着要重新构建一次。 |
| `BESTTEAM_DEMO_PIPELINES` **确认关闭** | FAIL | 不关的话，组织里的每个用户都能看到、能跑那几个演示用的 AI 团队。 |
| `BESTTEAM_EMAIL_*` 没有设置 | WARN | 这几项是给"整个进程配一个邮箱"用的；多客户场景应该用命令行 `admin set-email` 给每个客户单独连邮箱。 |
| 在建组织**之前**先设置好 `BESTTEAM_RUN_RETENTION_DAYS`（比如 `90`） | WARN | 不设的话，这个组织的运行记录会一直保留下去，而且已经建好的组织事后不会自动补上这条设置。 |
| `BESTTEAM_SENTRY_DSN` 已设置 | WARN | 不设的话，出了问题只能靠翻容器日志，没有别的记录渠道。 |
| `BESTTEAM_SENTRY_DSN` 是一个合法地址 | FAIL | 格式不对会导致程序在初始化阶段直接报错，陷入"启动—崩溃—重启—再崩溃"的死循环。 |
| `FORWARDED_ALLOW_IPS` 设成了你的反向代理地址 | WARN | 不设的话，前面提到的"按地址限流"额度会被反向代理后面的所有用户共用。 |

**它只检查格式，不检查值填得对不对——要看它回显出来的内容，别只看等级。** 一
个原封不动留着 `.env.example` 示例值的地址（`https://app.example-customer.com`）
本身格式完全合法，照样打印 `[OK]`。这一项还特别能藏：如果前端和后端是同一个域
名、由同一个反向代理分发的，浏览器对同源请求根本不做 CORS 检查，所以
`BESTTEAM_CORS_ORIGINS` 填错了在那之前什么毛病都不会有——等到哪天你把前后端拆
成两个域名，所有接口请求会在同一瞬间全部失败，而后端日志里一行记录都没有。

等组织建好之后：用 `--test` 参数连上它的邮箱（§4c）；如果是 Microsoft 365，
上线前对着真实租户走一遍 `docs/email-smoke-test.md` 第 9 节，最好客户在场一起
确认。最后把 `docs/BETA_NOTES.md` 交给客户。

## 2. 构建并启动

```bash
docker compose build
docker compose up -d
```

后端镜像是按 `requirements.lock` 这个"锁定文件"里写死的版本号安装依赖包的，所
以哪怕是紧急热修复重新构建一次，用的也是和上一次构建完全一样的依赖版本，不会
因为"今天刚好有个新版本发布了"而意外变化。真的要升级依赖版本，只能通过专门更
新这个锁定文件的操作来做（见 README 的"Updating the lockfile"一节）。

下面这些事，容器已经在 `Dockerfile` / `docker-compose.yml` 里帮你配好了，不用
你操心：

- **两个服务崩溃或者服务器重启之后都会自己起来**（配置里写的是
  `restart: unless-stopped`），只有你自己主动执行过 `docker compose stop`，它
  才会保持停止状态。后端还带了一个"健康检查"（`/api/health`），会去 ping 一下
  数据库，连不上就返回 503——所以 `docker compose ps` 显示的状态是
  `healthy`/`unhealthy`，不只是简单的"Up"；前端配置了
  `depends_on: condition: service_healthy`，所以只有后端确认健康了，前端才会
  开始对外提供服务。**要注意：普通的 Docker 并不会因为容器状态是
  `unhealthy` 就主动重启它**——`restart: unless-stopped` 只在进程真的退出的
  时候才起作用，"不健康"和"进程退出"是两回事。所以看到 `unhealthy` 要自己去查
  一下，不会自动好；如果想要那种自动重启的效果，需要额外装一个专门做这个的
  组件。另外，这个健康检查不会去比对数据库版本号是否是最新——一个正在跑数据库
  迁移的进程，虽然还没迁移完，但也算是"健康"的，不应该被当成故障。
- **后端容器是用一个权限受限的账号跑的**（用户名 `app`，用户 ID 是 1000），
  只有存数据的那个目录（SQLite 数据库文件和知识库上传的文件，对应
  `bestteam_data` 这个 Docker 卷）是可写的。如果这个数据卷是之前用"以 root 身
  份运行的旧镜像"创建的，那目录的所有者会是 root，新镜像的普通账号写不进去。
  换到这个新镜像之前，先手动执行一次：
  ```bash
  docker compose run --rm --no-deps --user root backend chown -R 1000:1000 /app/ui/backend/data
  ```
- **每次启动都会自动跑一遍数据库迁移**（`docker-entrypoint.sh` 会在启动
  `uvicorn` 之前先执行 `alembic upgrade head`；只有这个自动流程会这样，如果你
  自己手动执行
  `docker compose run backend python -m ui.backend.admin ...` 这种命令，是按你
  给的原样跑的——不会先自动帮你迁移一次，所以下面第 3 节里那些"恢复"用的命令，
  不会被"数据库还没迁移完"这件事卡住，反而正好是用来修复这个问题的）。
- **容器日志会自动轮转、清理**（后端最多留 5 份、每份 20 MB，前端最多留 3
  份、每份 10 MB）；具体去哪看日志、系统会上报哪些错误，见后面"日志与错误上
  报"一节。
- 后端容器被限制最多用 **2 GB 内存**；如果你打算开启"本地重排序模型"这个功能，
  要先把 `deploy.resources.limits` 这一项调大。
- **上传文件大小的限制应该配在你的反向代理上**，不是前端这个 nginx 上：浏览器
  是直接连后端的 8000 端口的，一份知识库的文档，或者一段面试录音，最大能到
  200 MB，这个大小必须能顺利穿过挡在最前面的那一层（反向代理），不然还没到后
  端就先被挡掉了。

`docker compose` 会自动从项目根目录读取 `.env` 文件，去替换
`docker-compose.yml` 里写的 `${VITE_API_BASE}` / `${VITE_WS_BASE}`（这跟后端读
`.env`是两件独立的事，后端是通过 `env_file: .env` 这行配置读的）。所以第 1 步
里填好的那些值，会在构建前端镜像的时候被写死进去。

## 3. 数据库迁移

后端容器每次启动的时候都会自己跑一遍 `alembic upgrade head`（见上一节），所以
正常情况下——不管是第一次启动，还是拉取了新版本、`alembic/versions/` 目录下多
了新文件之后再执行 `docker compose up -d`——这一步不需要你手动做任何事。但如
果你想在不启动整个服务的情况下单独跑一次迁移，或者想单独看一下这次迁移打印了
什么内容，可以自己手动执行：

```bash
docker compose run --rm --no-deps backend alembic upgrade head
```

"迁移"（migration）是这套系统创建、更新数据库结构的标准方式（取代了直接调用
`Base.metadata.create_all()` 这种一次性建表的做法——不过 `create_all()` 现在
还留着，作为一个"万一数据库完全是空的"情况下的兜底保护，正常情况下它什么都不
会做）。

**为什么要在真正对外提供服务之前先跑迁移。** `create_all()` 这种建表方式，不
会给一张已经存在的表加上新的索引或约束——所以一条由迁移引入的"安全规则"（目前
唯一的一条是"一个组织最多只能有一个成员"），在 `alembic upgrade head` 真正跑
完之前是不生效的。为了兜底，只要这条规则被违反了，后端会**直接拒绝对外提供
HTTP 服务**（具体恢复步骤见下面）——这样不管走哪条路，都不会出现"规则还没生效
但系统已经在正常服务"这个中间状态的窗口期。

### 恢复一个"旧模式"下的多成员组织

如果你的数据库是很早以前、"一个组织可以有多个成员"这个旧规则还生效的时候建
的，那可能还留着这样的组织。"一个组织只能有一个成员"这条迁移**会拒绝执行**
（并且会把有问题的组织名字打印出来），而不是自作主张删掉账号；出于同样的原
因，后端也会拒绝对外提供 HTTP 服务。因为主服务这时候是起不来的，恢复操作要在
一个"临时用一次就丢"的容器里跑（用 `run --rm --no-deps`，不要用 `exec`）：

```bash
# 先看看是哪些组织有问题：启动报错或者迁移报错里会直接点名。
# 然后针对每一个多出来的账号，要么直接删掉……
docker compose run --rm --no-deps backend python -m ui.backend.admin delete-user <用户名>
# ……要么把它挪到别的空组织，或者升级成平台管理员：
docker compose run --rm --no-deps backend python -m ui.backend.admin move-user <用户名> --to-org <另一个组织>
docker compose run --rm --no-deps backend python -m ui.backend.admin move-user <用户名> --platform

# 等每个组织都最多只剩一个成员了，再执行迁移，然后正常启动：
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d
```

## 4. 建组织、建账号（用命令行工具）

这套系统**没有对外开放的注册功能**——不管是界面上还是接口上都没有。组织和账号
都必须由你用命令行工具主动创建（记得先确认第 3 步的迁移已经跑完）：

```bash
# 一个客户对应一个组织。如果只服务一个客户，可以直接用系统自带的"default"组
# 织，不需要执行 create-org。
docker compose exec backend python -m ui.backend.admin create-org acme --display-name "示例公司"

# 给组织建一个成员账号（会提示你输入密码；--org 不填的话默认是 "default"）：
docker compose exec backend python -m ui.backend.admin create-user alice --org acme
# 注意：目前系统限制"一个组织最多一个成员"——create-user 建第二个成员会被拒
# 绝（因为组织级别的资源，比如共用的那个邮箱，现在还没有做到成员之间的权限
# 区分）。平台管理员账号不受这条限制。

# 给你自己建一个平台管理员账号（不属于任何组织）：
docker compose exec backend python -m ui.backend.admin create-user op --platform

# 列出所有组织：... python -m ui.backend.admin list-orgs
```

## 4b. 把第一个账号设为管理员

新建的账号默认都**不是**管理员。几个管理专用的页面——**账号管理**、**高级设
置**、**记忆库**、**运行轨迹**——只有管理员才能看到，而"谁是管理员"这件事只能
用命令行工具去改（不会因为写在某个环境变量列表里、或者用户名匹配上了什么规则
就自动变成管理员）：

```bash
docker compose exec backend python -m ui.backend.admin promote op
# 查看当前所有管理员：... python -m ui.backend.admin list
# 撤销管理员权限：     ... python -m ui.backend.admin demote <用户名>
```

只有"平台账号"（也就是用 `create-user --platform` 建的、不属于任何组织的账
号）才能被设为管理员：因为管理员能看到**所有**组织的配置，所以系统会拒绝把某
个组织的普通成员设为管理员——如果确实要给某个人管理员权限，给他单独建一个不
属于任何组织的账号。

管理专用的页面（对应接口是 `/api/config`、`/api/memory`）是跨组织的——具体改
哪个组织的东西，要在请求里用 `?org=<组织名>` 明确指定。普通组织成员用的页面
（引导流程、运行 AI 团队）必须是一个属于某个组织的账号；如果平台管理员自己也
想跑 AI 团队，需要另外给自己建一个属于某个组织的账号。

## 4c. 给每个组织连上自己的邮箱

邮件相关的几个工具（`email_find` / `email_read` / `email_read_attachment` /
`email_draft_reply`）**每个组织**只读**一个**邮箱——也就是说每个客户的 AI 团
队，只能碰到那一个客户自己的邮箱，碰不到别人的。这一步需要第 1 步里配好的
`BESTTEAM_SECRETS_KEY`——所有组织的邮箱密码，都是用这同一把部署级别的钥匙加
密的；密码本身加密后存进数据库，钥匙本身完全不进数据库。

**客户自己也可以在"团队搭建向导"里连自己的邮箱。** 客户搭建一个要用到邮件功
能的 AI 团队时，向导会出现一步"连接你的邮箱"（在"预览"阶段是可选的，方便客户
拿自己的真实邮箱先试试看；到"正式部署"这一步就是硬性要求了——走的是
`org/email` 这组接口，需要客户自己组织的账号登录）。所以下面这条命令行主要是
用在"你替客户先把前期准备做好"的场景；日常使用中，客户自己就能连。

```bash
# 普通 IMAP + 应用专用密码（会提示你输入密码；--test 会先试着登录一次，成功
# 才会真正保存）。一定要用应用专用密码，不要用账号本身的登录密码。
docker compose exec backend python -m ui.backend.admin set-email acme \
  --host imap.gmail.com --user support@acme.com --test

# Microsoft 365 / Exchange Online（会提示你输入客户端密钥）。不用填 --host：
# Exchange Online 的 IMAP 地址是固定的。客户 IT 那边要先做好的准备工作见下面
# "Microsoft 365 邮箱"一节。
docker compose exec backend python -m ui.backend.admin set-email acme \
  --auth microsoft-oauth --user support@acme.com \
  --tenant <目录ID> --client-id <应用ID> --test

# 断开邮箱连接：
docker compose exec backend python -m ui.backend.admin clear-email acme
```

### Microsoft 365 邮箱

Exchange Online（也就是企业版 Outlook 邮箱背后的服务）已经不接受传统的"账号+
密码"这种登录方式了，所以这类邮箱**不存在"应用专用密码"这种东西**，普通 IMAP
的连接方式一定会被拒绝。它们改用一种叫"应用级 OAuth 授权"的方式来连（技术上是
IMAP 协议里的 SASL XOAUTH2）。

这套设置只需要做一次，而且是在**客户自己的 Azure 租户里**做的——这一步这个平
台没办法替客户完成：

1. 在 Entra ID（也就是 Azure AD）里注册一个应用。记下**目录（租户）ID**和
   **应用（客户端）ID**，并创建一个**客户端密钥**。
2. 给这个应用添加权限：**Office 365 Exchange Online → 应用程序权限 →
   `IMAP.AccessAsApp`**，并授予管理员同意。
3. 在 Exchange Online 的 PowerShell 里，把这个应用注册为服务主体，并只给它这
   一个邮箱的访问权限：
   ```powershell
   New-ServicePrincipal -AppId <应用ID> -ServiceId <对象ID>
   Add-MailboxPermission -Identity <邮箱地址> -User <对象ID> -AccessRights FullAccess
   ```
4. 建议再加一步：用 Exchange 的**应用访问策略**（Application Access
   Policy）把这个应用限定在这一个邮箱上，这样哪怕凭证泄露，也碰不到租户里的
   其他邮箱。

之后客户在向导里填入邮箱地址、租户 ID、客户端 ID 和客户端密钥就行了（或者你用
上面那条 `--auth microsoft-oauth` 命令替他们填）。客户端密钥的加密方式跟 IMAP
密码完全一样，用的也是 `BESTTEAM_SECRETS_KEY`。**要注意 Azure 的客户端密钥是
有有效期的**（一般是 6 到 24 个月）：一旦过期，这个组织所有的自动运行都会开始
报"邮箱类"的错误，在"自动化"页面上能看到，修复方式是去 Azure 重新生成一个密
钥，再重新连一次。

如果连接失败了，报错信息会区分是哪种原因，修复方式也不一样：**令牌（token）
被拒绝**，说明租户 ID、客户端 ID 或密钥填错了；**令牌本身没问题但邮箱被拒
绝**，说明第 2 步或第 3 步没做完整。

上面这条路是"一套系统服务多个客户"场景下用的；`BESTTEAM_EMAIL_*` 这几个环境
变量，仍然是给 SDK/CLI 或者单客户部署用的"只配一个邮箱"的路子，在多客户部署
下会被**直接拒绝**（一个邮箱不可能安全地被多个客户共用）。一个还没连邮箱的组
织，工具会明确提示"没有连接邮箱"，不会报一个含糊的错误。

### 自动运行（邮件自动触发）

客户的邮件类 AI 团队正式部署、邮箱也连好了之后，客户可以自己选择开启（在"部
署"页面勾选"收到新邮件时自动运行"），开启之后系统会每隔
`BESTTEAM_TRIGGER_POLL_SECONDS`（默认 120 秒）去查一次新邮件，有新邮件就自动
跑一次这个 AI 团队——不需要人手动点一下。为了安全，加了几道保险：
`BESTTEAM_TRIGGER_DAILY_CAP` 限制每个组织每天最多自动运行多少次（默认 50 次，
超过之后要等到 UTC 时间的午夜才会重新开始计数）；`BESTTEAM_TRIGGERS_DISABLED=1`
是留给运维的一个"全局急停开关"。自动运行会出现在这个组织的活动记录里，署名是
`email-trigger` 这个系统账号；不管怎么运行，AI 团队都**只会存草稿**，不会真的
替客户把邮件发出去。去重是靠邮件的 IMAP UID（一个邮件唯一编号）来判断的，起点
是客户开启这个功能的那一刻，所以邮箱里原来积压的旧邮件不会触发自动运行。详细
设计见 `docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md`。

每一次自动运行最多处理 `BESTTEAM_TRIGGER_BATCH_SIZE` 封邮件（默认 20 封），而
且只处理这一批，不多不少；如果一下子涌进来的邮件比这个数量还多，会分成好几轮
慢慢处理，不会漏、也不会重复处理。**后端必须只用一个进程/一个 worker 来跑：**
自动收发邮件这个轮询机制、以及它自带的"防止重叠"保护，都是设计成在同一个进程
内部生效的，如果开了多个 ASGI worker，每一个都会各自去轮询，可能导致同一封邮
件被处理两次。让多个进程互相协调（"选主"）这件事目前还没做；在那之前，只能保
证只跑一个 worker——而且后端自己也在强制这一点：启动的时候会对数据库文件旁边
的 `<数据库文件名>.lock` 这个文件加一把独占的操作系统级锁（代码在
`ui/backend/process_lock.py`），所以如果针对同一个数据库启动第二个进程（比如
`uvicorn --workers N` 或者又起了一个副本），会直接拒绝启动并给出清楚的报错，而
不是悄悄把数据搞乱。这把锁会在进程退出时被操作系统自动释放，所以哪怕进程是异
常崩溃退出的，也不会卡住下一次启动。

如果 `BESTTEAM_TRIGGER_*` 这几个值填得不对（不是数字，或者是零/负数），后端会
直接拒绝启动并报清楚的错误，而不是等到运行的时候才悄悄发现轮询没在跑。

### 自动化出问题时的告警（按组织区分）

一个自动运行开始出问题之后，系统现在会主动说出来。告警会显示在客户界面的
**活动 → 告警**里，每个组织还可以额外配一个 Webhook 地址（**活动 → 告警 →
告警发送到哪里**），让告警同时推送到那个地址。有四种情况会触发一次告警：

| 触发条件 | 什么时候触发 |
|---|---|
| AI 团队连续运行失败 | 连续失败达到 `BESTTEAM_TRIGGER_ALERT_THRESHOLD` 次之后（默认 3 次，最低可以设成 1 次） |
| 邮箱连不上 | 同样是达到上面那个阈值 |
| 一次运行被"卡死运行监控"强制释放 | 立刻触发——因为这次运行已经卡住超过了完整的运行超时时长 |
| Microsoft 365 的客户端密钥快过期了 | 分别在还有 30 天、7 天，以及正式过期那天各提醒一次 |
| 收到的邮件在排队、处理不过来 | 最老的一封未处理邮件已经等了超过 `BESTTEAM_BACKLOG_ALERT_MINUTES`（默认 30 分钟，最低可以设成 1 分钟）——这种情况不是运行失败了，只是处理速度跟不上（常见原因是碰到了每日上限或者预算上限） |

告警只在"状态发生变化"的时候发，不是"问题一直存在就一直发"：一个问题被上报之
后，会保持沉默，直到这个问题解除，解除的时候会再单独通知一次"已恢复"。邮箱检
测恢复正常，只会清除"邮箱连不上"这一类告警，跟"AI 团队还能不能正常运行"是两
件事，互不影响。

Microsoft 365 密钥的过期日期，是管理员在连接邮箱的时候手动填的（可选项；
Azure 在你复制密钥的界面旁边就会显示这个日期）。这里**故意不**去 Entra 自动
读取这个日期——因为那需要 `Application.Read.All` 这个权限，等于能读到整个租
户里所有应用注册的信息，权限范围太大了。如果没有手动填这个日期，就不会有"密
钥快过期了"这类告警。

**Webhook 的技术细节。** 系统会用 `POST` 方法发送请求，`Content-Type` 是
`application/json`，请求头里带着 `X-BestTeam-Delivery: <通知ID>`；如果你配置
了签名密钥，还会带上 `X-BestTeam-Signature: sha256=<十六进制字符串>`——这是对
整个请求体做的 HMAC-SHA256 签名。只要你返回的是 2xx 状态码就算成功；否则系统
会在后面几次轮询里重试，最多重试五次，五次都失败之后就标记为"发送失败"，不过
在系统界面里还是能看到这条记录。

```json
{
  "id": 12,
  "org_id": 3,
  "kind": "trigger_health",
  "severity": "error",
  "title": "Automatic email replies are failing",
  "body": "The last 3 automatic runs failed, so no replies are being drafted.",
  "fingerprint": "workflow",
  "created_at": "2026-08-17T09:14:00+00:00"
}
```

`fingerprint` 这个字段是**存进数据库里的原始值**，之前系统内部把
"Workflow"这个概念改名叫"Pipeline"的时候，这个字段**故意没有跟着改**：它是
用来跟"改名之前就已经存在的记录"做比对的，如果把这个字符串也改了，会导致所有
历史告警重新被判定为"新告警"，全部再发一遍。接口路径和配置项的名字倒是跟着一
起改了。

校验一次 Webhook 推送是否真的来自本系统（Python 示例）：

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-BestTeam-Signature"])
```

推送内容里**只包含"健康状态"这类信息，绝不会包含邮件的主题、发件地址或者正文
内容**。Webhook 地址必须是 HTTPS，而且必须是一个公网能访问到的地址：这个告警
机制不是给你打通到部署所在内网的一条通道，所以不支持推送到你自己内网里的地
址。系统本身没有、以后也不会有通过邮件发送这类通知的功能——这是刻意的设计决
定：这个产品从头到尾都没有配置任何 SMTP（发邮件）的能力。

### 反过来监控"轮询这件事本身还在不在运转"（`check-health`）

上面说的这些告警，全都是由轮询邮件的那个循环自己发出来的——所以它们没办法覆盖
最重要的那种故障：**轮询本身，或者整个后端进程，已经卡死或者已经挂了**。这种
情况的检查，必须从进程**外面**来做：

```bash
docker compose exec backend python -m ui.backend.admin check-health
```

每个组织打印一行结果（FAIL / WARN / OK），内容是：轮询延迟了多久、邮件积压了
多久、过去 24 小时失败了多少次、从"发现新邮件"到"生成草稿"隔了多久——这些数据
都是轮询过程自己顺手记下来的，这条命令只是读出来做个判断。只要有任何一项是
FAIL（轮询延迟超过三个轮询周期，且不低于 5 分钟），这条命令就会以失败状态退
出。建议把它放进服务器的定时任务（cron）里定期跑，退出状态接到你原本就在用的
监控/告警系统上，比如：

```
*/10 * * * * docker compose -f /srv/bestteam/docker-compose.yml exec -T backend \
  python -m ui.backend.admin check-health || <这里换成你自己的通知方式>
```

`scripts/check-health-cron.sh` 就是这条定时任务的脚本版，已经按 cron 的脾气写好
（`-T`、`docker` 用绝对路径、退出码原样传出、只在失败时往
`/var/log/bestteam-health.log` 写一段）。在 crontab 那一行里设上
`BESTTEAM_OPS_WEBHOOK_URL`，失败时还会把一条 JSON（同时带 `text` 和 `content`
两个字段）POST 过去，Slack、Discord 这类 incoming webhook 不用改就能显示。接好
之后要故意停一次后端、看到日志里真的落下一段，这条链路才算验证过
（`docs/PRELAUNCH_DRILLS_RUNBOOK.md` §5.1.5）。

它只是读数据，不会写：如果数据库文件根本还不存在，它会直接说明这一点，然后以
成功状态（0）退出，不会自己去创建一个空数据库。

### 保留和清理运行记录（按组织区分）

一个会读邮件的 AI 团队，跑出来的运行记录里会带上客户的真实内容——姓名、邮件主
题、别人写的原文片段。邮件原文在存进系统之前已经做过脱敏处理了，但 AI 团队给
出的"答案"本身没办法脱敏，因为那就是这个产品交付的东西。能控制的只有"留多
久"。

每个组织在**活动 → 数据**这个页面自己设置保留期限：可以选"永久保留"（默认就
是这个），也可以选 30 / 90 / 180 / 365 天。在客户自己选一个期限之前，什么都
不会被删——升级这套软件本身，绝不会顺带删掉任何客户已有的历史记录。
`BESTTEAM_RUN_RETENTION_DAYS` 这个环境变量，只影响**新建**的组织一开始的默认
设置；已经存在的组织不会被这个设置追溯性地改动。

**清理会删掉什么**——那次运行的输入和输出内容、每一步的详细执行轨迹、以及自动
化运行里提取出来的具体结果。
**清理会保留什么**——"这次运行发生过"这个事实、发生时间、花费了多少、以及"哪
些邮件已经生成过回复草稿了"这条记录。最后这一条不是妥协出来的，而是有实际用
途：如果要重试一次失败的自动运行，系统靠这条记录来判断，避免给同一封已经回复
过的邮件又生成一份重复的草稿。

三个实际操作时要知道的点：

- **开启清理之前，先导出一份。** 同一个页面上的"下载导出"按钮（或者接口
  `GET /api/org/export`）会把一次清理将要删掉的内容，原样导出成 JSON 文件。
  单次导出的记录数上限是 `BESTTEAM_EXPORT_MAX_RUNS`（默认 5000 条），超过这个
  数量的导出结果里会带一个 `"truncated": true` 的标记，这样就不会把"导出了一
  部分"误当成"导出了全部"。
- **清理是跟着邮件轮询的节奏走的**，所以从"到了该清理的时间"到"真正清理掉"，
  最多相差一个轮询周期。设置 `BESTTEAM_TRIGGERS_DISABLED=1` 只会暂停自动运
  行，**不会**暂停清理——"先别自动运行了"和"先别删数据了"是两个不同的决定，不
  应该被一个开关同时控制。
- **清理不等于安全擦除。** SQLite（存数据用的那个文件数据库）在没有真正被覆
  盖写入之前，被"删除"的旧数据其实还留在磁盘的底层存储结构里，除非执行一次
  `VACUUM`（压缩整理）操作——而这套系统目前没有在任何地方自动执行这个操作。
  对于"我们不打算再保留这份数据了"这个目标来说，现在的做法是够用的；但如果面
  对的是一个已经拿到了数据库文件本身的攻击者，这种程度的清理是不够的。

按需求单独删除：**活动 → 运行记录 → 某一条运行 → 删除这条运行的内容**（对应
接口 `POST /api/runs/{id}/purge`），或者在"数据"页面点"立即清理"，删掉某个时
间点之前的所有记录。**系统不提供"按邮件地址删除"这个功能**——邮件地址本身没有
被单独建立索引存起来，只是散落在模型生成的自由文本里，可能还被模型换了种说法
复述过，所以按地址去匹配删除，既会漏删，也可能删多。要跟客户讲清楚这一点，不
要暗示这个功能"其实是支持的"。

### 个人记忆库（可选功能，默认关闭）

系统可以记住客户组织里的最终用户——他们之前问过什么、说过自己有什么偏好——在
多次运行之间接着用；管理员在**记忆**这个页面上可以查看、搜索和清理系统记住的
内容。这个功能默认是关的：只有 `BESTTEAM_MEMORY_DB` 指向了一个 SQLite 文件才
会启用。所以刚部署好的实例打开那个页面，看到的是"本部署未启用记忆功能"这句提
示，而不是一份列表——这是正常状态，不是出错。关着的时候其他一切照旧，AI 团队
的运行行为完全不受影响。

要打开它，把这个变量指向**数据卷上的一个路径**，然后重建后端容器：

```bash
# 写在 .env 里
BESTTEAM_MEMORY_DB=/app/ui/backend/data/memory.db
```

```bash
docker compose up -d --force-recreate backend
```

这个文件第一次用到的时候会自动创建，**不要**自己手动去建。路径是关键：只要不
在 `/app/ui/backend/data` 底下，这个文件就只存在于容器内部，下一次重建容器时
连同里面记住的所有内容一起消失。另外，光改 `.env` 对**已经在运行**的容器没有
任何影响（见后面"日志与错误上报"一节里的那条警告）——真正让它生效的是上面那条
重建命令。

只设这一个变量，用到的是最省的那一档：只存下运行过程中的原始交互内容，靠
BM25 关键词检索召回，**不会产生任何额外的模型调用**。再设
`BESTTEAM_MEMORY_MODEL` 才会在每次运行时多出一次抽取调用，把原始交互提炼成
"这个用户是个什么情况、有什么偏好"这类能长期留存的结论。建议先不设，等最省的
那一档确实跑出价值了再考虑。`.env.example` 里剩下那些 `BESTTEAM_MEMORY_*` 配
置项（向量混合召回、查询扩写、重排序），都是在这之上再叠加的可选项，而且每一
项都会带来每次运行的额外开销。

打开之前，有三件事需要先知道：

- **刚打开的时候，页面是空的。** 只有"带着用户身份"的运行发生过之后才会有记
  录，重启本身不会产生任何内容。重建完立刻去看是空列表，这是预期结果。
- **组织的运行记录保留期限管不到它。**"活动 → 数据"清理的是运行历史；记忆是
  另一个独立的库，有它自己的开关：`BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER`
  （不设 = 全部保留，不做上限）。删除一个账号**确实**会连带清掉这个账号的记忆
  内容，但前提是执行命令行工具的时候带上了服务端那套环境变量，让它能看到后端
  写入的是同一个库。详见 `docs/ADMIN_GUIDE.md` 第 6 节。
- **这个库里存的是最终用户自己说过的话**，性质上和运行历史是同一类内容，所以
  这个页面只有管理员能进，客户组织的成员看不到。"这些内容要留多久"这件事，要
  在打开之前就跟客户谈清楚，不要等打开之后再补。

## 5. 验证部署是否成功

- `curl http://localhost:8000/api/health` → 应该返回
  `200 {"status": "ok", "database": "ok"}`（这个接口是公开的，不需要登录；如
  果返回 `503 {"status": "degraded", "database": "error"}`，说明后端连不上它
  的 SQLite 数据库文件）。
- `curl http://localhost:8000/api/pipelines` → 应该返回 `401`（因为没带登录凭
  证，理应被拒绝）。
- `curl http://localhost:8000/api/pipelines -H "Authorization: Bearer <access_token>"`
  → 带上正确的登录凭证后，应该返回 `200`。
- 用浏览器打开前端地址——应该会被自动跳转到登录页 `/login`。用上面建好的账号
  登录，登录成功后，页面导航栏上应该能看到一个"退出登录"的链接。具体落在哪个
  页面取决于这个账号的情况：`/` 这个入口地址，对一个普通组织成员来说，如果这
  个组织已经有部署好的 AI 团队，会跳到 `/activity`（工作台）；如果这个组织还
  什么都没部署，会跳到 `/wizard`（搭建向导）；对平台管理员，会跳到
  `/advanced`（高级设置）。

## 升级一个已有的部署

**`./scripts/deploy.sh` 把下面整套步骤合成一条命令**：备份、拉代码、列出
`.env.example` 新增的变量、`check-env`（有 FAIL 就停在这一步，旧容器照常服务）、
构建、启动、等健康检查通过、再跑一次 `check-env`。下面把步骤写开，是为了让你知道
它在做什么，以及它中途停下时该怎么手动接着做。

升级就是在**服务器上**拉新代码、重新构建，顺序如下——顺序本身很重要，因为检查
`.env` 这一步，只有在新代码真的拉下来之后才有意义：

```bash
cd /opt/bestteam
./scripts/backup-db.sh /var/backups/bestteam/pre-upgrade-$(date +%F).db

git status                               # 先看看这台服务器上改过哪些文件
git stash && git pull && git stash pop   # 保住你在服务器上改过的东西（比如端口绑定）

# 这次升级有没有要求填新的环境变量。这条命令要在 git pull 之后跑：
# pull 之前，HEAD 还是你正在跑的那份旧代码，diff 出来是空的。
# ORIG_HEAD 是 git pull 自动记下的"拉之前你在哪个提交"，所以不用自己记上次
# 部署的版本号（也可以写死：git diff v0.1.0-beta.1 HEAD -- .env.example）。
git diff ORIG_HEAD HEAD -- .env.example

# 如果 diff 里有新增的变量名，手动加进你现有的 .env——**不要**重新执行 cp。
# 然后让 check-env 来把最后一道关：
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env

docker compose build
docker compose up -d                     # 数据库迁移是容器自己跑的，见第 3 节

# 起来之后，确认这次升级真的落地了：
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
docker compose exec backend printenv BESTTEAM_RELEASE
```

**为什么先跑 `git status`**：服务器上要是一个文件都没改过，`git stash pop` 会
以退出码 1 报 `No stash entries found`——无害，但看起来像是升级失败了。确认干
净的话，这一步直接 `git pull` 就够了。另外，那种"以后每次拉代码都必须保住"的
本机改动（典型的就是把端口绑到 127.0.0.1），更稳妥的做法是挪进
`docker-compose.override.yml`：compose 会自动合并这个文件，任何 git 操作都抹
不掉它。

`.env` 在 `.gitignore` 里，所以 `git pull` 永远不会碰它，上面那条 diff 只是
用来告诉你"要补哪几个变量"。真正的兜底是 `check-env`（见 §1）：它读的是后端
实际会看到的那份环境配置，只要有一项 FAIL 就以失败状态退出——漏填的变量会在
容器起来之前就暴露出来，而不是等到线上"启动—崩溃—重启"才发现。

**`up -d` 之后要再跑一次 `check-env`。** 它那行 `schema: at head (<版本号>)`
就是用来确认"容器启动时的 `alembic upgrade head` 确实跑过了"的。如果你只在容
器起来之前问一次，它报的是你**正要离开**的那个版本号——看起来跟"迁移被跳过了"
一模一样。

**还要分清你问的是哪个容器。** `docker compose run` 每次都新起一个一次性容器、
读当前的 `.env`，所以它只能证明**文件**是对的；`docker compose exec` 进的才是
真正在对外服务的那个容器。只有 `exec` 能证明线上后端确实读到了你改的值——这正
是让一个过期的 Sentry DSN 藏起来的同一个坑（见"日志与错误上报"）。

## 已有部署上，怎么升级"内置技能"

内置技能（比如 `email_triage_reply` 这个邮件分诊回复的技能）**只有在数据库里
还没有这一行记录的时候**才会在系统启动时自动写入——也就是说，这个自动写入的
过程绝不会覆盖一条已经存在的记录，所以管理员手动改过的内容永远不会被自动覆盖
掉。反过来也有个后果：当新版本的系统改进了某个内置技能，但一个已经在跑的部
署数据库里已经有这条记录了，那这个部署会继续用旧版本，不会自动更新。

要在一个已有的部署上用上新版本的内置技能，你需要拿到这次更新里**新的**技能
定义——注意，管理界面里显示的是**当前存在数据库里的（旧的）**那一份，直接打
开、保存并不会把内容换成新的。先用这条命令把当前代码里自带的最新版本打印出
来：

```bash
docker compose exec backend python -c "import json; from ui.backend.skills import DEFAULT_SKILLS; print(json.dumps(next(s.to_raw() for s in DEFAULT_SKILLS if s.name == 'email_triage_reply'), indent=2))"
```

然后把打印出来的这段 JSON 粘贴进"高级设置"界面 → 技能 →
`email_triage_reply`，点**保存**（或者直接调用接口
`PUT /api/config/skills/email_triage_reply`，不带 `org` 这个查询参数——不带这
个参数，改动的是平台层面的默认值）。如果直接把这条记录删掉再重启服务，也会
重新写入同样的默认值。

**如果你在这个技能上已经做过本地定制修改，上面两种方式都会把你的修改覆盖
掉**——系统目前没有那种"自动识别是不是被改过、需要的话保留旧版本"的机制（这
需要给每条记录做版本管理，目前这个规模还用不上）。所以正确做法是：把刚才打
印出来的默认值，跟你数据库里存的当前值做个比对，手动把两边的改动合并到一
起。

## 数据存在哪里

这套部署拥有的所有数据，都存在一个叫 `bestteam_data` 的 Docker 命名卷里，挂
载在后端容器内的 `/app/ui/backend/data` 这个路径下。执行
`docker compose restart` 或者不带 `-v` 参数的 `docker compose down`，这些数
据都不会丢：

- `bestteam.db`——SQLite 数据库文件：组织、用户、AI 团队及其历史版本、技能、
  知识库切分后的内容片段、运行历史和执行轨迹、用量统计、邮件处理进度、各项
  设置。**这是唯一一份"没有它就没法恢复"的数据**，恢复的时候一定要有这个文
  件。它是以 WAL 模式运行的，所以后端进程运行期间，旁边会跟着两个文件
  `bestteam.db-wal` 和 `bestteam.db-shm`：**进程还在运行的时候，绝对不要直接
  拷贝或删除这个 `.db` 文件本身**，要用下面提到的脚本来做。
- `knowledge_base_uploads/<组织名>/<知识库名>/<版本号>/`——每个知识库背后原始
  上传的文档。检索功能是直接从数据库里查的，所以就算没有这些原始文件，检索
  照样能用；这些文件的用处是"重新建索引"，或者"想确认某条结果具体来自哪份文
  件"的时候用来查证。
- `builder_sessions/`——搭建向导里每个会话自己的临时工作目录（用来对照校验这
  个会话生成的方案的那个"原始来源"，大多数情况下是空的）。
- `memory.db`——个人记忆库，只有在你把 `BESTTEAM_MEMORY_DB` 指向这个卷上的某
  个路径、主动开启之后才会有（见第 4 节"个人记忆库"）；具体是哪个文件名，取
  决于你把变量指到了哪里。它是一个独立的 SQLite 数据库，和 `bestteam.db` 不
  是一回事，`backup-db.sh` 会把它当成第二个文件一起备份（见下面"备份与恢
  复"）。

## 日志与错误上报

**日志在哪看。** 两个容器都是把日志打印到标准输出，Docker 会把它们存成会自
动轮转的 json 格式日志文件（后端最多留 5 份、每份 20 MB，前端最多留 3 份、每
份 10 MB——对一个日志级别是 INFO 的活跃测试环境来说，大概够留一周左右）。用
`docker compose logs -f backend` 来看（可以加上 `--since 1h` 或者
`--tail 500` 这类参数缩小范围）；这些日志能扛过容器重启，但扛不过
`docker compose down -v` 或者整台服务器重装，所以如果你需要留更长时间，要么
在 `docker-compose.yml` 的 `logging:` 那里配置一个日志外送方式，要么用一个专
门读取 `/var/lib/docker/containers/*/*-json.log` 这类文件的日志采集程序。系
统自己打的每一条日志格式都是`时间戳 级别 模块名: 具体内容`；
`BESTTEAM_LOG_LEVEL` 这个环境变量可以调高或调低打印的门槛（默认是 INFO）。
Uvicorn 自己的访问日志是单独的一条，不受这个设置影响。

**一个可选的错误上报渠道。** 设置 `BESTTEAM_SENTRY_DSN`（Sentry 的免费额度
对一次测试运营来说够用；任何兼容 Sentry 协议的收集服务，比如 GlitchTip，也可
以用）之后，后端只会上报两种事件——一种是**没被妥善处理的接口异常**（也就是
客户在页面上看到的那个 500 错误），另一种是**一次运行本身失败了**（可能是
AI 团队自己的逻辑失败，也可能是后台工作线程崩溃了）——每条上报都会打上运行
ID、AI 团队名字、请求方法和路由模板（不会带具体的路径参数，因为有的参数可能
是一个分享链接的令牌）。异常的类型和堆栈信息会被上报，但异常的**具体文字内
容**不会（比如一条解析错误里可能直接引用了模型输出的原文，一条 HTTP 错误里
可能带着工具访问过的具体网址），运行失败的具体原因也不会——这两类信息，本来
就已经完整地记在这台服务器本地的运行轨迹里了，报告里只带上对应的运行 ID，方
便你按 ID 去查。除此之外什么都不会被上报：不会把 ERROR 级别的日志整个镜像过
去，不带请求体，不带程序运行时的变量快照，不做性能追踪，也不带任何用户数据
（`send_default_pii` 这个选项是关闭的）。这是刻意这样设计的——这套系统处理
的是客户的邮件和文档，任何一条上报出去的信息都必须是"就算离开了这台服务器也
不会有问题"的。如果 `BESTTEAM_SENTRY_DSN` 格式不对，后端会在启动阶段就直接
停下来（`check-env` 会最先把这个问题标出来）。`BESTTEAM_ENVIRONMENT`（默认是
`production`）和 `BESTTEAM_RELEASE` 这两项是给上报事件打标签用的；把这个
DSN 清空就是关闭错误上报。`sentry-sdk` 这个依赖包已经打进了容器镜像里；如果
你是直接用 `pip install` 装的（不走容器），它是 `ui` 这个可选依赖组的一部
分。

**配好之后验证一次。** `check-env` 只能证明 DSN 格式合法，证明不了事件真的送
得到。用真实的代码路径发一条出去：

```bash
docker compose run --rm --no-deps backend python -c "import sentry_sdk; from ui.backend import error_reporting as er; print('enabled:', er.init_from_env()); er.report_message('sentry smoke test', source='manual'); sentry_sdk.flush(timeout=5); print('sent')"
```

打印出 `enabled: True` 说明容器读到了 DSN，几秒之后 Sentry 的问题列表里就该出
现这一条。验证完把它标记成已解决，让列表平时保持是空的——以后冒出东西才是真
的需要看。另外，Sentry 引导页上给的那段 `sentry_sdk.init(...)` 示例代码**一行
都不要抄**：后端已经自己初始化过了，而且那段示例把 `send_default_pii` 设成了
打开，正好和上面整段描述的策略相反。

⚠️ **改了 `.env` 不会影响已经在运行的容器。** `docker compose run` 每次都是新
起一个一次性容器、读当前的 `.env`，所以上面这个冒烟测试可能是过的，而线上那个
后端容器里用的还是旧值。改完 `.env` 之后必须显式重建：

```bash
docker compose up -d --force-recreate backend
```

这一条对 `.env` 里的每个变量都成立，不只是 Sentry 这一项。

如果冒烟测试打印了 `sent`，Sentry 那边却什么都没有，先怀疑 DSN 本身——粘贴时
少了几位是最常见的原因。把 SDK 撇开，自己看 HTTP 响应码；SDK 的调试日志只在失
败时打印、成功时什么都不打，所以「日志里没报错」这个信息量比看上去小：

```bash
DSN=$(grep -E '^BESTTEAM_SENTRY_DSN=' .env | cut -d= -f2-)
KEY=$(echo "$DSN" | sed -E 's#^https://([^@]+)@.*#\1#')
HOST=$(echo "$DSN" | sed -E 's#^https://[^@]+@([^/]+)/.*#\1#')
PROJ=$(echo "$DSN" | sed -E 's#.*/([0-9]+)$#\1#')
echo "host=$HOST project=$PROJ key=${KEY:0:8}...(${#KEY} chars)"
curl -sS -i -X POST "https://$HOST/api/$PROJ/store/" \
  -H "Content-Type: application/json" \
  -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=$KEY, sentry_client=curl/1.0" \
  -d '{"message":"curl smoke test","level":"error","platform":"other"}'
```

`key` 应该正好 32 位，`project` 要和你在 Sentry 界面上看的那个项目对得上。返
回 `200` 并带一个 event id，说明接收端已经收下；返回 `401` 或 `403`，说明 key
或者项目 ID 不对——回项目的 **Client Keys (DSN)** 页面把整串重新复制一遍，别
手敲。

## 备份与恢复

一次备份是**两个文件**（开了个人记忆库的话是三个），由两个脚本分别生成，两
个脚本在后端正在运行的时候执
行也是安全的：

```bash
./scripts/backup-db.sh       # 备份数据库本身，走的是 SQLite 自带的"在线备份"接口
./scripts/backup-files.sh    # 备份数据卷上除数据库以外的所有内容，打成一个 .tgz 压缩包
# 也可以自己指定文件路径：
./scripts/backup-db.sh    /path/to/backups/bestteam-2026-06-17.db
./scripts/backup-files.sh /path/to/backups/bestteam-files-2026-06-17.tgz
```

这两个脚本之所以分开，是因为它们要处理的东西性质不一样：一个**正在被使用**
的数据库，必须通过 SQLite 专门提供的备份接口来复制（如果直接原样拷贝文件，
可能会拷到一个"写到一半"的数据页，导致备份文件本身是坏的）；而上传文件这一
块，就是普通文件，用 `tar` 打包完全没问题——所以 `backup-files.sh` 特意把
`bestteam.db` 以及它旁边的 `-wal` / `-shm` 文件排除在外，而 `backup-db.sh`
也完全不管数据库以外的任何东西。只靠数据库这一份备份，就能恢复出一套能正常
运行的部署；文件那份备份，恢复的是每个知识库背后的原始文档（具体哪些数据存
在哪里，见前面"数据存在哪里"一节）。

**个人记忆库是 `backup-db.sh` 顺带一起备份的。** 它是另一个独立的 SQLite 数
据库，走的是同一套在线备份接口，生成的文件就放在主备份旁边，名字是
`<主备份路径>-memory.db`。脚本是问**正在运行的那个容器**要
`BESTTEAM_MEMORY_DB` 的值（只改了 `.env` 但没重建容器的话，那个值并不是后端
实际在用的），所以定时任务那边不用加任何东西：没开这个功能、或者开了但还没
产生过内容的部署，脚本会直接说明情况，只备份数据库本身。压缩包那边也会把同
一个文件当成普通文件打包进去——**压缩包里那份就当作没有**，它正是"原样拷贝一
个正在被使用的数据库"，也就是上面这套拆分特意要避开的做法。

这一点在恢复的时候会咬人：`restore.sh` 是把文件压缩包整个解开、覆盖到数据目
录上的，所以它放回去的是压缩包里那份，不是好的那份。恢复的时候把好的那份作为
第三个参数交给它（见下面"要从备份恢复"），不要让压缩包里那份留在那儿。

**两个都要定时跑。** 容器本身不会自动帮你做任何备份。在宿主机（跑 Docker 的
那台服务器）上配一条每晚跑一次的 crontab 定时任务，对一次测试运营来说就够用
了（下面这条要按实际情况改路径；两个脚本都要从代码所在的目录下执行，
`docker compose` 才能找到项目）：

```cron
15 3 * * * cd /opt/bestteam && ./scripts/backup-db.sh /var/backups/bestteam/bestteam-$(date +\%F).db >> /var/log/bestteam-backup.log 2>&1 && ./scripts/backup-files.sh /var/backups/bestteam/bestteam-files-$(date +\%F).tgz >> /var/log/bestteam-backup.log 2>&1
```

旧的备份文件，用你已经在用的任何清理方式定期删掉就行（比如同一个 crontab 里
加一条 `find /var/backups/bestteam -mtime +30 -delete` 就是最简单的写法），
并且要把这个备份目录**另外拷一份到别的地方**——一份"跟数据库放在同一台机器
上"的备份，一旦这台机器出问题，这份备份也一起没了，那就不能算是真正的备份。

**跑脚本的那个账号，必须对输出目录有写权限。** 如果定时任务是挂在 `root` 下
的，`/var/backups/bestteam` 这个目录会是 root 建的、属主是 root；你后来用部署
用的普通账号手动跑同一条命令，会在**最后一步**失败并报 `permission denied`
——而这时候容器内部的那份副本其实已经做好了，脚本因为 `set -e` 直接退出，把它
留在了容器里的 `/tmp/bestteam-backup.db`。属主改一次就好
（`sudo chown <用户名> /var/backups/bestteam`），残留的那份顺手删掉
（`docker compose exec -T backend rm -f /tmp/bestteam-backup.db`）。

**把 `BESTTEAM_SECRETS_KEY` 单独备份，而且要放在安全的地方**（密码管理器或者
专门的密钥保管工具——**不要**跟数据库备份放在一起）。存进数据库里的邮箱密码是
用这把钥匙加密的，所以一份数据库备份，如果没有这把钥匙，对"恢复邮件功能"这件
事来说是没用的。如果这把钥匙丢了或者被改掉了，后端会拒绝启动（并且会点名是哪
几个组织受影响），不过**命令行工具本身还是能正常用的**——恢复方式是把受影响
的邮箱清空、重新连一次：

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin clear-email <组织名>
docker compose run --rm --no-deps backend python -m ui.backend.admin set-email <组织名> --host ... --user ... --test
```

目前还没有一条"原地换钥匙"的命令；要换掉这把钥匙，意味着要把每个组织的邮箱都
清空、用新钥匙重新连一遍。

要从备份恢复，执行恢复脚本，带上数据库文件；如果你还有文件那份备份、以及记忆
库那份备份，就一起带上：

```bash
./scripts/restore.sh /path/to/backups/bestteam-2026-06-17.db /path/to/backups/bestteam-files-2026-06-17.tgz /path/to/backups/bestteam-2026-06-17-memory.db
```

第三个参数是可选的，脚本会在**解开文件压缩包之后**才放它进去，正好覆盖掉压缩
包里那份 raw tar 拷贝。至于放到哪个路径，看的是 `.env` 里 `BESTTEAM_MEMORY_DB`
当前的值——脚本在停任何东西之前就先把这个值读出来，如果没设置就当场退出，而不
是把一份没有任何后端会去打开的数据库恢复到某个地方。

这个脚本会按顺序自动做完下面几步，最后会等着 `/api/health` 返回 200 才算结
束。**在第一个正式客户上线之前，一定要先演练一次**——找一套"用完就扔"的
`docker compose` 环境来练，不要直接在正式环境上试，这样真正需要恢复的那一次
才不会是你第一次做这件事。**后续代码更新，默认不需要重新演练一遍**——只有当
`scripts/restore.sh`、两个备份脚本、`docker-entrypoint.sh` 或
`docker-compose.yml` 这几个文件本身有改动，才需要重新演练。单纯新增一个
Alembic 迁移不算：那走的是第 2 节里"每次启动自动迁移"那条路径，靠正常升级流
程就能验证，不需要靠恢复流程来验证。如果你想每一步都自己手动确认一遍，对应
的手动流程是：

1. 先停掉后端，确保恢复过程中没有任何东西还在往数据库里写：
   ```bash
   docker compose stop backend
   ```
2. 删掉旧数据库旁边的 WAL/journal 相关文件（不删的话，SQLite 启动时会试图把
   这些文件里的内容重新"回放"到刚恢复进去的文件上，把恢复结果搞乱），然后把
   备份文件拷进容器，覆盖掉现在正在用的数据库：
   ```bash
   docker compose run --rm --no-deps --user root backend sh -c 'rm -f /app/ui/backend/data/bestteam.db-wal /app/ui/backend/data/bestteam.db-shm /app/ui/backend/data/bestteam.db-journal'
   docker compose cp /path/to/backups/bestteam-2026-06-17.db backend:/app/ui/backend/data/bestteam.db
   ```
   `docker cp` 这条命令拷贝文件的时候，文件所有者会是 **root**，而后端进程是
   用用户 ID 1000 跑的——这样的话后端能读这个刚恢复的数据库，但写不进去（也
   就没法给它跑迁移），所以启动之前要把所有者改回来：
   ```bash
   docker compose run --rm --no-deps --user root backend chown 1000:1000 /app/ui/backend/data/bestteam.db
   ```
   记忆库那份备份的放法完全一样——同样这三条命令，把路径换成
   `BESTTEAM_MEMORY_DB` 指向的那个文件——但必须放在文件压缩包解开**之后**，
   不能在之前。
3. 重新启动后端：
   ```bash
   docker compose start backend
   ```
4. 验证：`curl http://localhost:8000/api/health` 返回 `200`，并且用一个"备份
   之前就已经存在"的账号能正常登录。
