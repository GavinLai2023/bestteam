# 上线前演练 Runbook（Pre-launch Drills）

这份文档把「付费试点前必须在真实环境做一遍」的几件事，拆成可以照着抄命令执行的步骤。目标读者是**不熟悉 Docker/VPS 运维**的人——每一步都写明了「做什么」「为什么做」「怎么判断做对了」「出错了怎么办」。

## 0. 这份文档解决什么问题

`docs/deployment.md` 描述的是**正常上线流程**（每一步怎么做）。这份文档是**上线前的演练**（先在没有真实客户数据的情况下把最容易出问题的几件事验证一遍）。原因很简单：`scripts/restore.sh` 从来没有真正跑过一次的备份策略，不是备份策略；没验证过的 M365 接入流程，第一次给客户配置时才发现哪里少一步，客户会看在眼里。

四件事，缺一不可：

| # | 演练 | 验证什么 | 对应 STATUS/记忆里的欠账 |
|---|------|----------|--------------------------|
| 1 | 强杀 → 重启 → 恢复 | 进程崩溃后不会永久卡死邮件、不会丢数据、单实例锁能正常工作 | kill→restart→recovery drill |
| 2 | 备份 → 恢复（restore rehearsal） | `scripts/restore.sh` 真的能把一个备份恢复成可用的部署 | project_beta_freeze 里欠的 "restore rehearsal" |
| 3 | M365 真实租户 smoke test + 密钥轮换 + 权限撤销 | 代码路径和 Azure 官方要求真的对得上；同时把「怎么给客户配 M365」这件事走一遍，变成可复用的 onboarding 步骤 | project_beta_freeze 里的 "G7" |
| 4 | 收尾：check-health 上线 + 设置留存期 | 监控真的在跑；不会有 org 永久保留邮件内容 | PR #93 上线后的运维动作 |

**建议执行顺序**：1 → 2 → 3 → 4。原因：演练 1 和 2 只需要一个空的测试部署，做起来最快，也最容易发现「Docker/VPS 环境本身有没有配对」这类基础问题；演练 3 涉及 Azure 后台操作，需要预约时间、可能要等待权限生效，放最后；演练 4 是把前面都验证完之后的收尾动作。

**在哪里做**：必须在**目标 VPS** 上做（不能在本地开发机上，因为本地机器大概率没有 Docker，而且这份演练本身就是在验证「部署环境」）。全程使用一个**测试用的 org/用户**，不要用真实客户的数据——即便这台 VPS 就是将来给客户用的那台也没关系，只要还没有真实客户上线，跑完演练后把测试数据清掉即可（第 5.5 节有清理步骤）。

---

## 1. 开始前：确认基础环境已经就绪

如果这台 VPS 还没有跑起来过，先完成 `docs/deployment.md` 的第 0–2 节（配置 `.env`、`docker compose build && docker compose up -d`）。做完后，用下面几条命令确认基础是稳的——**后面所有演练都建立在这个前提上**，跳过这一步会让后面的问题很难判断到底是演练本身的问题还是环境本身没配对。

```bash
cd /opt/bestteam   # 换成你实际的部署目录

# 1. 容器都在跑，backend 显示 healthy（不是只有 Up）
docker compose ps

# 2. 健康检查直接过
curl http://localhost:8000/api/health
# 期望： {"status": "ok", "database": "ok"}

# 3. 启动检查全绿（这条命令只读，不会创建数据库）
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
# 期望：所有行都是 [OK] 或 [WARN]，没有 [FAIL]
```

如果 `check-env` 有 `[FAIL]`，先按提示把 `.env` 改对，重新 `docker compose up -d`，直到这一步全部通过，再继续下面的演练。

**创建一个用完就删的测试账号**（后面几个演练都要用它登录/操作）：

```bash
docker compose exec backend python -m ui.backend.admin create-org drilltest --display-name "Drill Test"
docker compose exec backend python -m ui.backend.admin create-user drilluser --org drilltest
# 会提示你输入密码，随便设一个，记下来
```

---

## 2. 演练 1：强杀 → 重启 → 恢复

**验证的问题**：如果这台服务器意外断电、进程被系统 OOM killer 杀掉，或者你自己手滑 `docker kill` 了容器，重新拉起来之后，系统会不会："卡在一半的邮件永远卡住"、"两个进程同时跑、产生重复草稿"、"锁文件把重启也一起锁死"？

### 2.1 制造一个「进行中」的状态

先让系统里有点「正在处理」的东西，这样重启后才有东西可以验证有没有被正确清理。最简单的办法：临时把邮件轮询间隔和运行超时都调得很短，制造一个会被中断的 run。**如果你现在还没有测试邮箱可用，跳到 2.4，只做「单实例锁」和「基本重启」两项也是有意义的验证**，M365 的部分留到演练 3 一起做。

如果已经有测试邮箱（哪怕是普通 Gmail 都行），走完整流程：

1. 参考 `docs/deployment.md` §4c，用 `set-email` 给 `drilltest` 这个 org 接一个测试邮箱：
   ```bash
   docker compose exec backend python -m ui.backend.admin set-email drilltest \
     --host imap.gmail.com --user your-test-address@gmail.com --test
   ```
2. 登录前端（`https://<你的域名>`，或没配域名时 `http://<VPS-IP>`——`docker-compose.yml` 里 frontend 发布的是 **80** 端口；`5173` 是本地开发的 Vite 端口，VPS 上没有），用 `drilluser` 登录，走 Team Builder 向导部署一个用到邮件工具的团队，并在 Deploy 页打开「自动运行」。
3. 给测试邮箱发一封新邮件。

### 2.2 强杀 backend

**记录下 kill 之前的状态**，方便对比：

```bash
docker compose exec backend python -m ui.backend.admin check-health
```

然后模拟一次「毫无征兆的崩溃」——不是优雅停止，是直接杀掉进程（`docker compose kill` 发的是 `SIGKILL`，跳过任何清理逻辑，这正是我们要测的最坏情况）：

```bash
docker compose kill -s SIGKILL backend
docker compose ps
```

> **不要期待看到 `Exited`。** backend 在 `docker-compose.yml` 里是 `restart: unless-stopped`——被 SIGKILL 打死之后 Docker 会**自动**把它拉回来（这条策略就是为此存在的：崩溃或主机重启后自己回来，只有显式 `docker compose stop` 之后才保持停止）。所以 `docker compose ps` 多半直接显示 `Up`，或者一闪而过的 `Restarting`。**没抓到退出瞬间不代表 kill 没生效**，判断依据是下一节的日志和状态，不是 `ps` 的手速。

### 2.3 重新拉起来，检查恢复情况

```bash
docker compose up -d backend   # 已经被 restart 策略拉起来的话，这条是幂等的
```

等它启动后（几秒钟），依次确认：

| 检查项 | 命令 | 期望结果 |
|---|---|---|
| 服务恢复健康 | `curl http://localhost:8000/api/health` | `200 {"status":"ok",...}` |
| 启动日志里能看到清理动作 | `docker compose logs backend --since 5m \| grep -i interrupted` | 能看到 `Marked N interrupted run(s) as failed on startup`（如果 kill 时刚好有知识库导入在跑，还会有一条 `Marked N interrupted knowledge-base ingestion job(s) as failed on startup`）。这两行**只在真的有东西要清理时才打印**——kill 时没有 running 的 run 就一行都没有，这也正常 |
| 单实例锁被正确释放（没有把自己锁死） | 上一步 backend 能正常启动本身就证明了这一点；如果反而报 `SingleInstanceError`，说明锁释放有问题，需要人工删除 `<db文件>.lock` 后再启动，并把这个现象记下来上报 | backend 正常 Up + healthy，不需要手动删锁文件 |
| 之前"进行中"的 run 没有卡在 `running` 状态 | 登录前端 Activity 页，或 `curl -H "Authorization: Bearer <token>" http://localhost:8000/api/runs` | 状态是 `failed`，不是永远 `running` |
| 邮件没有被卡住/丢失 | 检查测试邮箱的收件箱和草稿箱 | 之前发的测试邮件要么已经处理完（有草稿），要么还在等待下一轮轮询处理——不应该永久卡在"认领中"状态 |

### 2.4 验证单实例保护（这是这次改动专门加的安全网）

这是整套「单进程部署」设计的核心保护：**如果有人手滑用 `--workers N` 或者不小心启动了第二个进程指向同一个数据库，系统必须拒绝启动，而不是悄悄跑出两个邮件轮询器**。手动验证一次：

```bash
# 保持 backend 正常运行的情况下，尝试再起一个指向同一份数据库的进程
docker compose run --rm --no-deps backend python -m uvicorn ui.backend.main:app --host 0.0.0.0 --port 8001
```

**期望**：这个命令应该立刻失败，报错里应该提到 `<db>.lock`、"another backend process already holds"、"single-process by design" 这类字样。如果它反而正常启动了，这是一个严重问题，需要暂停演练并上报。

```bash
# 用完 Ctrl+C 结束这个临时进程（如果它确实报错退出了就不用管）
```

### 2.5 通过标准

- [ ] `SIGKILL` 后 `docker compose up -d backend` 能正常拉起，无需任何人工干预（不需要手动删锁文件、不需要手动改数据库）
- [ ] 之前 `running` 状态的 run 被清理为 `failed`，不会永远卡住
- [ ] 邮件没有丢失（要么处理完了，要么还在等下一轮，不会永久"认领中"）
- [ ] 手动尝试启动第二个进程时，系统明确拒绝并给出清晰的报错

全部打勾才算通过。任何一项不通过，先不要往下做演练 2/3，把问题记录清楚（日志、复现步骤）。

---

## 3. 演练 2：备份 → 恢复（restore rehearsal）

**验证的问题**：`scripts/restore.sh` 已经写好几个月了，但从来没有真的跑过一次。这个演练就是跑一次，确认它真的能用。

### 3.1 先确认"看得出有没有恢复成功"的数据

演练 1 里创建的 `drilltest` org 和 `drilluser` 就是"备份时已经存在"的那份数据——恢复之后它必须还在。这一步不需要再创建任何东西。

> **不要给 `drilltest` 再建第二个账号。**"一个 org 只有一个成员"是数据库层强制的不变量（`users.org_id` 上的部分唯一索引，加 `create_user` 的前置检查），不是约定——`create-user <另一个名字> --org drilltest` 会被直接拒绝，报 "Organization already has a member"。原因见 `docs/DECISIONS.md`"one member per org"：org 范围的资源（尤其是自助连接的共享邮箱）还没有成员级权限区分，在有 per-org 管理员角色之前不允许两个成员共管同一个邮箱。
>
> 所以下面用**新建一个 org** 来当"备份之后才发生的变化"的标记。

### 3.2 做一次备份

```bash
mkdir -p /tmp/drill-backup
./scripts/backup-db.sh    /tmp/drill-backup/bestteam.db
./scripts/backup-files.sh /tmp/drill-backup/bestteam-files.tgz
ls -la /tmp/drill-backup/
```

期望：两个文件都生成了，且大小不是 0。

### 3.3 制造"备份之后又发生了变化"的状态

这一步是为了验证恢复"确实是回到了备份那个时间点"，而不是"反正数据库没坏所以看起来像是成功了"：

```bash
docker compose exec backend python -m ui.backend.admin create-org afterbackup --display-name "After Backup"
docker compose exec backend python -m ui.backend.admin list-orgs
# 期望：输出里同时有 drilltest 和 afterbackup 两行
```

现在 `afterbackup` 这个 org 存在，但它是在备份**之后**创建的——恢复完成后，它应该**消失**（因为恢复回到了备份那一刻，备份之后发生的事情不存在）。

> 如果 `create-org` 报错 `BESTTEAM_EMAIL_BACKEND is set but this deployment has more than one organization`，说明这台部署还在用进程级邮件凭据（那条路径只支持单 org）。共享多租户部署本来就不该设 `BESTTEAM_EMAIL_*`——把它们从 `.env` 里去掉、改用 `set-email` 的按 org 凭据，重启后再跑这一步。

### 3.4 执行恢复

```bash
./scripts/restore.sh /tmp/drill-backup/bestteam.db /tmp/drill-backup/bestteam-files.tgz
```

这个脚本会自己完成「停止 backend → 清理旧的 WAL/journal 文件 → 拷贝备份进去 → 解压文件归档 → 把文件所有权交还给容器用户 → 启动 backend → 等待健康检查」全部步骤，最后会打印 `Restore complete: the backend is healthy.`。

**如果它没有在 60 秒内等到健康检查通过**，会打印错误提示你去看 `docker compose logs backend`——这种情况下不要慌，先看日志找原因（常见原因：备份文件路径错了、磁盘空间不够），修好后可以重新跑一次 `restore.sh`。

### 3.5 核对恢复结果

| 检查项 | 怎么验证 | 期望结果 |
|---|---|---|
| 服务恢复健康 | `curl http://localhost:8000/api/health` | `200` |
| 备份时已存在的账号还在 | 用 `drilluser` 的账号密码登录前端 | 能登录成功 |
| 备份之后才创建的 org 消失了 | `docker compose exec backend python -m ui.backend.admin list-orgs` | 输出里有 `drilltest`、**没有** `afterbackup`——**这一步是恢复真的生效的关键证据**，如果 `afterbackup` 还在，说明恢复根本没有真的替换数据库 |
| 数据文件也恢复了（如果测试时上传过知识库文档） | 检查对应的知识库文档还在 | 文档存在，可以正常检索 |

### 3.6 记录这次演练特有的两个未知点

`project_beta_freeze` 里提到，有两件事只有真正跑一次 `restore.sh` 才能确认，请在这次演练里顺手记录下来（不需要额外操作，只是留意结果）：

1. **`docker compose cp` 写入一个已停止的容器，是否真的写穿到了数据卷** —— 3.5 节如果验证通过，就说明写穿了，没问题；如果验证失败但脚本本身没报错，很可能是这个环节出了问题，需要进一步排查。
2. **文件恢复是"追加"而不是"替换"** —— `tar xzf` 没有删除语义，所以恢复文件归档之后，**备份之后新建的知识库上传目录不会被清理，会变成孤儿目录**（数据库已经回滚，不再引用它们）。这不是 bug，只是要知道：`restore.sh` 恢复完之后，如果想要一个"干净"的状态，可能需要手动清理这些孤儿目录。可以顺手确认一下：在 3.3 之后（备份之后）用 `drilluser` 再上传一份知识库文档，恢复完成后，数据库里已经查不到它，但它的文件目录大概率还留在磁盘上。

### 3.7 通过标准

- [ ] `restore.sh` 跑完打印 "Restore complete: the backend is healthy."
- [ ] 备份时已存在的账号能登录
- [ ] 备份之后创建的 `afterbackup` org 在 `list-orgs` 里已经消失（证明真的回滚了，不是假通过）
- [ ] 记录了上面两个未知点的实际观察结果

---

## 4. 演练 3：M365 真实租户 smoke test + 密钥轮换 + 权限撤销

**验证的问题**：如果客户的邮箱是 Microsoft 365（工作/学校账号），代码路径和 Azure 官方要求已经核对过是一致的，但**从来没有真正连过一个真实的 Azure 租户**。这个演练同时也是在把"给 M365 客户接入邮箱"这件事走一遍，走完之后你自己就有了一份可以照做的操作经验——这就是欠账里说的 "onboarding runbook"。

**前提**：

- 一个 Microsoft 365 **工作或学校**租户（个人 Hotmail/Outlook.com 账号走不通，见 `docs/email-smoke-test.md` §15）。没有的话可以申请 Microsoft 365 开发者订阅，免费，自带一个测试租户和若干测试邮箱。
- 该租户的**全局管理员**账号。下面每一步——注册应用、授予应用权限、Exchange PowerShell——都需要管理员权限，没有就做不下去。
- 一台能跑 PowerShell 的机器（你的 Windows 开发机就行，**不需要**在 VPS 上跑 PowerShell）。

**预计耗时**：Azure + PowerShell 那一侧第一次做大约 40–60 分钟（含等权限生效），连接和错误验证约 30 分钟，再加 4.8 那一小时的挂机观察。建议排半天，别和别的事挤在一起。

### 4.1 先搞清楚这几个名词（不用动手，但值得先看）

M365 这套东西最劝退的地方是名词多，而且**长得像但不是一回事**。看懂这张表，后面每一步在干什么就清楚了：

| 名词 | 是什么 | 在这次演练里对应 |
|---|---|---|
| 租户 tenant | 客户的整个 M365 组织。**Directory (tenant) ID** 是它的编号 | 你申请的那个测试租户 |
| 应用注册 App registration | 你创建的这个"应用"的定义。**Application (client) ID** 是它的编号 | 代表 BestTeam 这个程序 |
| 客户端密钥 client secret | 这个应用的"密码"，**会过期** | 4.3 里创建，之后要轮换 |
| 企业应用 Enterprise application / 服务主体 service principal | 同一个应用在**这个租户里的实例**。它有**另一个** Object ID | 4.4 的 `-ServiceId` 要的就是它，**不是应用注册的 Object ID** |
| `IMAP.AccessAsApp` 权限 | 允许应用以自己的身份用 IMAP，**但不限定哪个邮箱** | 4.3 第 4 步授予 |
| `Add-MailboxPermission` / Application Access Policy | 才是"限定到哪个邮箱"的东西 | 4.4 / 4.5 |

**为什么授权要分成两步**：`IMAP.AccessAsApp` 只说"这个应用可以用 IMAP"，不说"可以读谁的邮箱"。所以只做第一步的话，连接会在"拿到 token 之后、打开邮箱之前"失败——这也正是 4.7 表格里最值得验证的那一行。

> 整个流程用的是 **app-only（客户端凭据）**授权：没有用户登录、没有跳转页面、没有 MFA。代码只做一件事：拿 client id + secret 向 `login.microsoftonline.com/<tenant>/oauth2/v2.0/token` 换一个 scope 为 `https://outlook.office365.com/.default` 的 token，再用 SASL XOAUTH2 连 `outlook.office365.com:993`（`src/bestteam/tools/_oauth.py`）。host 和端口都是代码里写死的，向导不会让你填。

### 4.2 准备：装 PowerShell 模块，确认测试邮箱可用

在你自己的机器上开一个 **PowerShell 7**（5.1 也能用，7 更省心）：

```powershell
# 第一次要装模块，之后不用再装
Install-Module -Name ExchangeOnlineManagement -Scope CurrentUser
# 会问是否信任 PSGallery，选 Y

Connect-ExchangeOnline -UserPrincipalName <你的全局管理员账号>
# 会弹浏览器登录窗口，正常登录即可
```

连上之后，确认要用的那个测试邮箱**开着 IMAP**——这是最容易被忽略、报错又最难懂的一个前提：

```powershell
Get-CASMailbox -Identity <测试邮箱地址> | Format-List ImapEnabled
# 期望：ImapEnabled : True

# 如果是 False：
Set-CASMailbox -Identity <测试邮箱地址> -ImapEnabled $true
```

> 租户级别也可能整体关了 IMAP（`Get-CASMailboxPlan | Format-List Name,ImapEnabled`）。如果邮箱级已经是 True 但连接仍被拒，回头查这里。

### 4.3 在 Entra 里注册应用

进 [entra.microsoft.com](https://entra.microsoft.com)（或 portal.azure.com → Microsoft Entra ID）。微软会时不时调整菜单文字，下面写的是位置，看到差不多的字就对了。

1. **Applications → App registrations → New registration**
   - 名字随便起，比如 `BestTeam mailbox access (drill)`
   - Supported account types 选 **Accounts in this organizational directory only**（单租户）
   - Redirect URI **留空**——app-only 流程没有用户登录，不需要回调地址
   - 点 Register

2. 注册完会跳到 **Overview** 页，抄两个值（点右边的复制图标，别手打）：
   - **Directory (tenant) ID**，形如 `72f988bf-xxxx-xxxx-xxxx-2d7cd011db47`
   - **Application (client) ID**，也是一串 GUID

   > 这一页还有第三个 GUID 叫 **Object ID**。**它不是 4.4 要用的那个**，先别抄，抄了反而搞混。

3. **Certificates & secrets → Client secrets → New client secret**
   - Description 写点能认出来的，比如 `drill-2026-08`
   - Expires 选 **6 months**（演练用；真实客户也别选 24 个月，越长越容易忘）
   - 点 Add 之后表格里出现一行，有 **Value** 和 **Secret ID** 两列
   - **复制 Value 那一列**，不是 Secret ID。Value 只在这一刻能看到明文，**刷新页面后永远看不到**，抄错只能删掉重建
   - 同时**记下这一行的 Expires 日期**，4.6 场景 B 要填

4. **API permissions → Add a permission**
   - 上方切到 **APIs my organization uses** 页签（**不是** Microsoft APIs 页签，也**不在** Microsoft Graph 里面——这是最常走错的一步）
   - 搜 `Office 365 Exchange Online`，点进去
   - 选 **Application permissions**（不是 Delegated permissions）
   - 展开 **IMAP**，勾 **`IMAP.AccessAsApp`**，点 Add permissions
   - 回到列表页，点 **Grant admin consent for <你的租户名>** 并确认。**Status 那一列要变成绿色的 "Granted for ..."**，没变绿就是没生效，后面一定连不上

5. 现在去拿 4.4 真正要用的那个 ID：**Applications → Enterprise applications → All applications**，按名字搜到刚才这个应用，点进去，在 **Overview / Properties** 上抄 **Object ID**。

   > 这个 Object ID 和第 2 步 App registration 页上那个 Object ID **是两个不同的值**。`-ServiceId` 要的是**这一个**（企业应用／服务主体的）。填错是这个流程里最常见的失败，而且报错信息不会告诉你填错了哪一个。
   >
   > 如果 Enterprise applications 里搜不到这个应用：说明第 4 步的 admin consent 没点成功，回去补。

### 4.4 在 Exchange Online 里把这个应用授权到那一个邮箱

回到 4.2 那个已经 `Connect-ExchangeOnline` 的窗口：

```powershell
# 把这个应用登记成 Exchange 认识的服务主体
New-ServicePrincipal -AppId <Application (client) ID> -ServiceId <企业应用的 Object ID> -DisplayName "BestTeam drill"

# 确认登记成功
Get-ServicePrincipal | Format-List DisplayName,AppId,ServiceId

# 把这个服务主体加到目标邮箱的完全访问权限上
Add-MailboxPermission -Identity <测试邮箱地址> -User <企业应用的 Object ID> -AccessRights FullAccess

# 确认加上了——直接把 GUID 传给 -User，让 Exchange 自己解析这个对象
Get-MailboxPermission -Identity <测试邮箱地址> -User <企业应用的 Object ID>
# 期望：User 显示的是刚才起的 DisplayName（比如 "BestTeam drill"），AccessRights 里有 FullAccess
```

> **较新的 Exchange Online 模块已经把 `-ServiceId` 改名为 `-ObjectId`**（`New-ServicePrincipal` 执行完会打一条 WARNING 提示这件事）。如果你的模块是新版本，`Get-ServicePrincipal | Format-List DisplayName,AppId,ServiceId` 里 `ServiceId` 那一行会完全不显示——这不代表没注册成功，换成 `Format-List DisplayName,AppId,ObjectId` 查看即可；`New-ServicePrincipal` 命令本身的返回结果里也会直接打印 `ObjectId`，可以拿它和你填的 GUID 直接核对，不用再另外查一次。
>
> **不要用 `Get-MailboxPermission | Where-Object { $_.User -like "*<GUID>*" }` 来验证。** 权限加上之后，Exchange 会把这个服务主体解析成一个 SID（`UserSid`），`Get-MailboxPermission` 返回的 `User` 属性字符串形式是这个 SID，不包含原始 GUID——所以即便权限真的加成功了，按 GUID 字符串过滤也会查不出任何结果（假阴性，实测验证过）。上面 `-User <GUID>` 的写法是让 Exchange 自己去解析这个对象再返回结果，才是可靠的验证方式。
>
> **权限生效有延迟**，几分钟到十几分钟不等。4.6 第一次连接如果失败，**先去泡杯茶再试一次**，别立刻怀疑配错了——这是整个流程里最容易让人白折腾半小时的地方。

### 4.5 （强烈推荐）把这个应用锁死在这一个邮箱上

**为什么值得做**：`IMAP.AccessAsApp` 是租户级权限。只做 4.4 的话，一旦 client secret 泄露，攻击面是**整个租户的所有邮箱**；Application Access Policy 才是把它从租户级收窄到一个邮箱。真实客户的 IT 一定会问这个问题，**你得能当场答上来**。

```powershell
New-ApplicationAccessPolicy -AppId <Application (client) ID> `
  -PolicyScopeGroupId <测试邮箱地址> `
  -AccessRight RestrictAccess `
  -Description "BestTeam drill: this mailbox only"

# 验证：授权的邮箱应该是 Granted
Test-ApplicationAccessPolicy -Identity <测试邮箱地址> -AppId <Application (client) ID>
# 期望 AccessCheckResult : Granted

# 验证：租户里另一个邮箱应该是 Denied（这才是这条策略真正的价值）
Test-ApplicationAccessPolicy -Identity <另一个邮箱地址> -AppId <Application (client) ID>
# 期望 AccessCheckResult : Denied
```

> 这条策略同样**最多要等 30 分钟**才完全生效，`Test-ApplicationAccessPolicy` 的结果可能比实际生效更快变绿。
>
> 微软正在把 Application Access Policy 迁移到 **RBAC for Applications**（`New-ManagementRoleAssignment -App ...`）。如果你的租户上 `New-ApplicationAccessPolicy` 已经不能用了，改用 RBAC for Applications 的等价做法，作用一样——**把这件事发生了记下来**，它会直接影响你给客户 IT 的那份操作单怎么写。

### 4.6 用这几个值连接（两种场景都走一遍）

**场景 A：操作员 CLI（你替客户配置时用的）**

> **这一步要切回 VPS 的终端。** 4.2–4.5 全程是在你自己电脑的 PowerShell 里对着 Azure/Exchange 操作，跟 Docker 无关；到这一步开始要用到部署本身，`docker` 命令必须在**目标 VPS** 上执行。如果你在本地 PowerShell 里直接敲 `docker compose ...` 会报 `docker: The term 'docker' is not recognized`（本地机器八成没装 Docker）——先 `ssh` 进 VPS，`cd` 到部署目录（比如 `/opt/bestteam`），再执行下面的命令。

```bash
docker compose exec backend python -m ui.backend.admin set-email drilltest \
  --auth microsoft-oauth --user <测试邮箱地址> \
  --tenant <Directory (tenant) ID> --client-id <Application (client) ID> --test
```

会**分两次**提示你输入 client secret（`Client secret:` 和 `Repeat client secret:`，都不回显，也不留在 shell 历史里）。`--test` 会在保存前做一次真实连接验证——**不带 `--test` 就是盲存**，一定要带上。

> **实测过的两种结果，供对照：**
> - 密钥填错（比如抄成了 Secret ID 而不是 Value）：命令会报错退出，不保存，提示形如 `error: Login test failed, not saved: Microsoft rejected the application's sign-in (401): AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID, for a secret added to app '<client id>'. Trace ID: ... Correlation ID: ... Timestamp: ...`——注意 CLI 这里直接把微软原始的 AADSTS 报错整段打出来了（带 Trace ID/Correlation ID），比 4.7 表格里前端向导那条经过包装的短提示更啰嗦，但信息是一致的：密钥不对。
> - 密钥和其他值都对：命令打印 `Connected mailbox '<测试邮箱地址>' for organization '<org>'.`，正常退出。

> **`set-email` 没有录入密钥到期日的参数**（只有 `--auth/--host/--user/--tenant/--client-id/--port/--drafts/--test`）。而到期告警只对**记录了到期日**的凭据生效——所以只走场景 A 配置的 org，**永远不会收到密钥即将过期的提醒**。4.3 第 3 步记下的那个日期，只能在场景 B 的向导里填。如果这台部署将来主要靠 CLI 代客户配置，把这一点当作已知缺口记下来。

**场景 B：客户自助向导（客户实际会看到的流程）**

用 `drilluser` 登录前端，走到 Team Builder 的「Connect your mailbox」步骤：

1. 认证方式选 **Microsoft 365 / Outlook (Exchange Online)**（另一个选项 "Standard mailbox (IMAP)" 对 M365 永远连不上，Exchange Online 已经不接受基本认证了）
2. 填 **Email address**、**Directory (tenant) ID**、**Application (client) ID**、**Client secret**
3. **Secret expiry date (optional)** 填 4.3 第 3 步记下的日期——填了才会有到期提醒
4. 点 **Test connection**

> 向导里**不会**问 IMAP 服务器和端口：M365 模式下这两个值在代码里固定成 `outlook.office365.com:993`。看不到这两栏是正常的。

两种场景都走一遍：客户大概率走 B，而你替客户补救／代配置时走 A。

### 4.7 故意配错，确认每一种失败都有"看得懂的提示"

**这是整个演练里最值得花时间的部分**。客户自己撞上这些错误时能不能看懂提示，直接决定了他们是自己修好，还是发一封"连不上"给你然后干等。

依次**只改错一个值**（改完点 Test connection 或重跑 `set-email --test`），确认提示对得上。第三列是代码里的原文（`ui/backend/org_settings.py`）：

| 改错哪个值 | 应该命中的判断 | 实际提示（英文原文） |
|---|---|---|
| Directory (tenant) ID 改错 | 租户不认识 | "Microsoft didn't recognise that Directory (tenant) ID. Copy it from the app registration's Overview page in the Azure portal." 后面附微软自己那句 AADSTS |
| client secret 改错（或 Application (client) ID 改错） | 应用身份不通过 | "Microsoft didn't accept the application's sign-in. Check the Application (client) ID, and that the client secret is correct and hasn't expired." |
| 凭据全对，但**跳过 4.4** 的 `Add-MailboxPermission`（可以临时 `Remove-MailboxPermission` 制造这个状态） | token 拿到了，但邮箱打不开 | "Microsoft accepted the app's sign-in but refused it access to '<邮箱地址>'. Ask your IT administrator to grant admin consent for the IMAP.AccessAsApp permission, then register the app against this mailbox in Exchange Online (New-ServicePrincipal, then Add-MailboxPermission)." |
| 邮箱地址填一个**本租户里不存在**的地址 | 同上一行 | 同上，且提示里**点名了你填的那个地址** |

**第三行是重点**："token 能拿到、但邮箱访问被拒"和"密钥填错了"，从微软返回的原始错误里几乎分不出来。代码是**先单独取一次 token、再连邮箱**（`_mailbox_problem`），才有可能把这两种情况分开——因为它们的修法完全不同：一个是回去改密钥，一个是让客户 IT 补一条 PowerShell。如果实测下来这一行的提示和"密钥填错"看起来一样含糊，**这是要上报的问题**，不是可以将就的小瑕疵。

改完记得把值改回正确的，再往下做。

### 4.8 端到端跑一次，并开启自动轮询

把 4.6 连上的邮箱接一个真实团队并跑一次：

> **一个 org 只能有一个邮箱、一个自动运行的团队**（`email_credentials` 和 `email_triggers` 都在 `org_id` 上唯一，写入是 upsert）。所以 4.6 用 `drilltest` 连 M365，会**静默覆盖**演练 1 里连的 Gmail；下面打开自动运行，也会**静默把演练 1 那个团队的自动运行关掉**——不会报错，这是预期行为，不是 bug。如果想让两者共存，给 M365 演练单开一个 org（`create-org drilltest-m365` + `create-user`）。

1. 给测试邮箱发一封新邮件。
2. 手动运行一次这个邮件团队，确认 **Drafts 里出现了回复草稿**，**Sent 里没有任何东西被发出**——这是整个产品的核心安全属性：只写草稿，绝不自动发送。顺手翻一下 Sent，别只看草稿箱。
3. 在 Deploy 页打开「自动运行」，再发一封新邮件，确认**几分钟内**自动出现新草稿，不需要人工点运行。默认轮询间隔 **120 秒**（`BESTTEAM_TRIGGER_POLL_SECONDS`），正常两三分钟内就该有反应。
4. **放着跑超过 1 小时**，中途再发一两封邮件，确认仍然正常出草稿。这一步测的是 **token 刷新**：access token 大约 1 小时过期，代码在过期前 **60 秒**主动换新（`_oauth.py` 的 `_EXPIRY_MARGIN_SECONDS`）。**如果恰好在开始后一小时前后开始失败，就是刷新没工作**——这个 bug 只有真的等满一小时才会暴露，别跳过。

> 顺带留意：自动运行每天有上限（`BESTTEAM_TRIGGER_DAILY_CAP`，默认 50 次），到顶会暂停到 UTC 零点并给出"已达每日上限"的提示。演练量级碰不到，但看到这个提示时要知道它是什么。

### 4.9 密钥轮换演练

模拟"client secret 快过期了，要换一把新的"。Azure 的 client secret 通常 6–24 个月过期，**这件事一定会在客户的生命周期里发生**，所以这个流程必须是你闭着眼睛能做的。

1. 回到 Entra 应用注册 → **Certificates & secrets**，给**同一个应用再创建一个新的 client secret**。
   **先加新的，验证通过之后再删旧的**——反过来会出现一段"两边都不能用"的空窗期，客户那段时间的邮件全都不会被处理。
2. 用新 secret 重跑场景 A 或场景 B 的连接步骤（`set-email --test`，或向导里重新填一次）。
3. 确认连接测试通过、自动轮询继续正常出草稿。
4. **可选但推荐**：把旧密钥删掉制造一次真实的失效，确认该 org 在 **Activity → Alerts** 里收到告警，再补上新密钥验证恢复。
   **注意这里会看到哪一种告警**——系统里是两条互不相干的链路，别当成一个：
   - **"Your mailbox can't be reached"**（`trigger_health.py`）：由**连接连续失败**触发，删掉密钥测的就是这一条。要**连续 3 次**轮询失败才发（`BESTTEAM_TRIGGER_ALERT_THRESHOLD`，默认 3），按 120 秒的间隔算大约 **6 分钟**，不是一个周期。
   - **"Your Microsoft 365 app password expires soon / has expired"**（`email_trigger.sweep_secret_expiry`）：由**记录在案的到期日**触发（到期前 30 天、7 天各一次），跟能不能连上无关。**删掉 Azure 里的密钥不会触发它**，而且只有走过场景 B、填了到期日的凭据才有这条。
5. 在 Azure 里删掉旧的 client secret，轮换完成。

**记录下来**：从"决定轮换"到"轮询恢复正常"实际花了多长时间、几步操作。这就是将来那份轮换 SOP 的原始素材。

### 4.10 权限撤销演练

模拟客户方 IT 收回授权（客户离职、审计要求、或者客户决定不用了）：

1. 在 Exchange Online PowerShell 里撤销邮箱权限：
   ```powershell
   Remove-MailboxPermission -Identity <测试邮箱地址> -User <企业应用的 Object ID> `
     -AccessRights FullAccess -Confirm:$false
   ```
   更彻底的做法（三选一，**做过一次就够，别三种都试**，否则 4.11 清理时容易乱）：撤销 `IMAP.AccessAsApp` 的 admin consent（Entra → Enterprise applications → 这个应用 → Permissions → Revoke），或者直接删掉整个 App registration。
2. 等**至少 3 个**轮询周期（约 6 分钟，见 4.9 步骤 4），确认：
   - 该 org 的 **Activity → Alerts** 里出现了 **"Your mailbox can't be reached"**
   - 系统**没有**因此崩溃，**其他 org 不受影响**（这台是共享多租户实例，一个客户的邮箱坏掉不能拖垮别人——这才是这一步真正在验证的东西）
   - 手动触发一次连接测试（`set-email --test` 或向导里的 Test connection），错误提示能让人看懂是"权限被收回了"，而不是一个含糊的失败

### 4.11 清理

```powershell
# 如果上一步没删，把邮箱权限和服务主体清掉
Remove-MailboxPermission -Identity <测试邮箱地址> -User <企业应用的 Object ID> -AccessRights FullAccess -Confirm:$false
Remove-ServicePrincipal -Identity <企业应用的 Object ID>
# 如果建过 Application Access Policy（先用 Get-ApplicationAccessPolicy 查 Identity）
Remove-ApplicationAccessPolicy -Identity <策略的 Identity>
Disconnect-ExchangeOnline -Confirm:$false
```

然后在 Entra 里删掉这次的 **App registration**（删掉它，对应的企业应用条目也会一起消失），避免租户里留下一个没人管、但仍然有效的应用注册。

### 4.12 通过标准

- [ ] 4.3–4.5 的 Azure/Exchange 设置全部亲手走完，每一步都**看到了实际界面／命令输出**（不是照抄文档、心里觉得"应该没问题"）
- [ ] 分得清 App registration 的 Object ID 和企业应用的 Object ID，`New-ServicePrincipal` 用对了后者
- [ ] 4.7 的四种错误配置，每一种提示都对得上表格（尤其第三行能和"密钥填错"区分开）
- [ ] 端到端收到真实草稿，**Sent 里确认没有任何邮件被发出**
- [ ] 自动轮询连续跑过 1 小时以上没中断（token 刷新正常）
- [ ] 完整走过一次密钥轮换，且记录了实际耗时和步数
- [ ] 权限撤销后 "Your mailbox can't be reached" 在预期时间内出现，其他 org 不受影响
- [ ] 测试用的 App registration／服务主体／邮箱权限都已清理
- [ ] 把 4.3–4.10 的实际步骤、耗时、踩到的坑整理成一份给客户 IT 看的简版操作单（**这就是欠账里说的 onboarding runbook**——不用另外写文档，把这次的真实记录整理一下就是它）

### 4.13 常见故障速查

| 现象 | 最可能的原因 | 怎么确认／怎么修 |
|---|---|---|
| 提示 "didn't recognise that Directory (tenant) ID" | tenant ID 抄错，或抄成了别的 GUID | 回 App registration 的 Overview 页重新复制 |
| 提示 "didn't accept the application's sign-in" | client secret 抄成了 **Secret ID** 而不是 **Value**；或者 secret 已过期 | 删掉重建一个 secret，创建后立刻复制 Value 那一列 |
| 提示 "refused it access to '<邮箱>'" | admin consent 没点，或 `New-ServicePrincipal` 的 `-ServiceId` 填成了 App registration 的 Object ID | 看 API permissions 里 Status 是不是绿的；`Get-ServicePrincipal` 查出来的 ServiceId 和 Enterprise applications 页上的 Object ID 对不对得上 |
| 刚配好就失败，等十分钟后自己好了 | 权限生效延迟 | 正常现象，4.4 的提示里说过 |
| Enterprise applications 里找不到这个应用 | admin consent 没成功 | 回 API permissions 重点一次 Grant admin consent |
| 连接通过，但一写草稿就失败 | 草稿箱名字没被识别 | 代码靠 IMAP 的 `\Drafts` 特殊标志自动探测，Exchange Online 正常都带；万一不行，用 `set-email --drafts "<草稿箱实际名字>"` 手工指定 |
| 一切都对但就是连不上 | 邮箱的 IMAP 被关了 | 回 4.2 跑 `Get-CASMailbox ... ImapEnabled` |
| 跑满一小时后开始失败 | token 刷新有问题 | **这是真 bug，要上报**，附失败时间点和 `docker compose logs backend` |

---

## 5. 收尾：check-health 上线 + 设置留存期 + 清理测试数据

演练之外、但同样属于「上线前必须做完」的收尾动作。前面三个演练验证的是**系统扛不扛得住**，这一节做的是**出事的时候你会不会知道**，以及**别把演练留下的垃圾数据交给真实客户**。

顺序有讲究，**不要跳着做**：5.5 会删掉测试账号，而 5.4 的留存期只有 org 成员登录后才能设置——账号删了就再也设不了了。

### 5.1 把 check-health 接入 cron

这是整个部署里**唯一一个从进程外面看进程死活**的东西，值得多花十分钟做对。

**为什么必须从外面看**：产品里所有的告警（邮箱连不上、密钥要过期、积压太久）都是**由邮件轮询循环自己投递的**（`email_trigger.run_maintenance`）。所以轮询器本身卡死、或者整个进程挂掉的时候，它没有任何办法告诉你——这恰恰是最要命的那种故障：客户的邮件一封都不会被处理，而系统一声不吭。`check-health` 从容器外面跑，就是为了补这个洞。

#### 5.1.1 先手动跑一次，看懂输出

```bash
cd /opt/bestteam
docker compose exec backend python -m ui.backend.admin check-health; echo "exit=$?"
```

它对**每一个开着自动运行的 org** 打印四行，最后一行是汇总。典型输出：

```
[OK]   poll[drilltest]: checked 43s ago
[OK]   backlog[drilltest]: no messages waiting
[OK]   runs[drilltest]: 3 message(s) completed, none failed in the last 24h
[OK]   latency[drilltest]: detection to draft: p50 51s, max 78s over the last 24h
no failures, 0 warning(s)
```

四行分别是什么，以及**什么时候它不是 OK**：

| 行 | 含义 | 不是 OK 的条件 |
|---|---|---|
| `poll[org]` | 距离上一次「检查完这个邮箱」过了多久 | 超过 `max(3 × 轮询间隔, 5 分钟)`——按默认 120 秒算就是 **360 秒**——报 **FAIL**；从来没检查过一次报 WARN |
| `backlog[org]` | 有几封邮件在排队、最老的等了多久 | 最老的超过 `BESTTEAM_BACKLOG_ALERT_MINUTES`（默认 30 分钟）报 **WARN** |
| `runs[org]` | 24 小时内处理完 / 失败的邮件数 | 有失败就报 **WARN** |
| `latency[org]` | 从发现邮件到写出草稿的 p50 / 最大耗时 | 只报数字，永远不会 FAIL |

**只有 `poll` 那一行会 FAIL，也只有 FAIL 会让退出码变成 1**（WARN 不影响退出码，`admin.py:_print_findings`）。换句话说这个 cron 巡检盯的是**一件事**：轮询器停摆或进程已死。

> **它不负责发现「邮箱连不上」。** 连接失败时轮询器照样会记下「我检查过了」（`last_checked_at` 在失败分支里也会更新），所以 `poll` 那一行仍然是 OK。凭据失效、权限被撤销这类问题走的是另一条路——客户自己在 **Activity → Alerts** 里看到的 "Your mailbox can't be reached"（演练 3 §4.9 / §4.10 验证过的那条）。两条链路分工不同，别指望一条覆盖另一条。

#### 5.1.2 为什么用 `exec` 而不是 `run --rm`

前面 `check-env` 用的是 `docker compose run --rm --no-deps`，这里换成了 `exec`，不是笔误：

- `exec` 是**进正在运行的那个容器**执行。容器要是根本没起来，这条命令直接非零退出——而「整个进程死了」正是这个巡检最该抓到的情况，所以这个失败是我们想要的。
- `run --rm` 会**另起一个容器**，主进程已经死了它照样能连上数据库把指标算出来（那时 `poll` 会 FAIL，也能报警），但多绕了一层。
- 这条命令**不会**和单实例锁打架：锁是 `main.py` 启动 FastAPI 时拿的，admin CLI 只开一个数据库会话、不碰锁，所以每 10 分钟跑一次没有任何副作用（`ui/backend/process_lock.py`、`admin.py:_open_session`）。

#### 5.1.3 写成一个脚本，别写成 crontab 一行

crontab 里的一行有几个专坑新手的地方：`%` 有特殊含义、引号和 `$(...)` 容易被吃掉、写错了没法单独测。**做成脚本这些问题一个都不存在**，而且可以先手动跑一遍确认它好使。

```bash
sudo tee /usr/local/bin/bestteam-health-check.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
# Cron watchdog for the email poller. Writes to the log only when the check
# fails, so an empty log means "everything was fine every time".
set -uo pipefail

DEPLOY_DIR=/opt/bestteam
LOG=/var/log/bestteam-health.log

cd "$DEPLOY_DIR" || { echo "=== $(date -Is) cannot cd to $DEPLOY_DIR" >> "$LOG"; exit 1; }
output=$(/usr/bin/docker compose exec -T backend \
  python -m ui.backend.admin check-health 2>&1)
status=$?

if [ "$status" -ne 0 ]; then
  {
    echo "=== $(date -Is) check-health exit=$status"
    echo "$output"
  } >> "$LOG"
fi
exit "$status"
EOF

sudo chmod +x /usr/local/bin/bestteam-health-check.sh
sudo touch /var/log/bestteam-health.log
```

几个细节，改路径的时候别改坏了：

- **`-T` 不能省**。cron 没有终端，不加 `-T` 的 `docker compose exec` 会因为分配不到 TTY 而失败——那样你收到的每一条告警都是假的。
- **`docker` 写绝对路径**。cron 的 `PATH` 很短（通常只有 `/usr/bin:/bin`），你在交互式 shell 里能跑的命令，cron 里未必找得到。先确认位置：`command -v docker`，把输出填进脚本。
- **`DEPLOY_DIR` 必须是放着 `docker-compose.yml` 的那个目录**，`docker compose` 靠当前目录找项目。
- 脚本最后**把 `check-health` 的退出码原样传出去**。日志是这里的告警渠道，但将来要换成别的（监控 agent、webhook、推送）时，直接拿退出码就行，不用改脚本。

先手动验证脚本本身：

```bash
sudo /usr/local/bin/bestteam-health-check.sh; echo "exit=$?"
sudo cat /var/log/bestteam-health.log
```

期望：`exit=0`，日志文件是空的（健康时脚本什么都不写）。

#### 5.1.4 加进 crontab

```bash
sudo crontab -e     # 用 root 的 crontab：docker 一般需要 root 或 docker 组权限
```

加两行：

```
MAILTO=""
*/10 * * * * /usr/local/bin/bestteam-health-check.sh
```

- **`MAILTO=""` 是有意为之**：cron 默认会把命令的输出**发邮件**给该用户，而这台机器上没有 MTA（本项目刻意不带任何 SMTP，见 `docs/DECISIONS.md`），这些邮件只会烂在 `/var/mail` 里或者直接丢掉。脚本自己写日志，所以把 cron 的邮件关掉。
- **如果你坚持不用脚本、要写成一行**：记住 **crontab 里未转义的 `%` 会被当成换行符**，命令会从那里被截断。所以一行里出现的 `%` 全都要写成 `\%`（5.2 备份那一行里的 `date +\%F` 就是这个原因）。**这个反斜杠只在 crontab 里需要，写进脚本文件反而是错的**——复制粘贴时别把它带过去。

确认这条 cron 真的在跑（不是「日志没内容所以应该在跑」）：

```bash
sudo crontab -l                        # 确认那两行在
sudo journalctl -u cron --since -1h    # Debian/Ubuntu；也可以看 /var/log/syslog
# 期望：每 10 分钟一条 CMD (/usr/local/bin/bestteam-health-check.sh)
```

#### 5.1.5 制造一次真实失败，确认告警链路是通的

**这一步是这一节的重点**。健康的时候日志是空的，所以「日志一直没内容」既可能是「一切正常」，也可能是「这条 cron 从头到尾就没跑起来」——两者长得一模一样。必须人为坏一次，看它响不响。

**测试 A：整个进程死掉**

```bash
docker compose stop backend
sudo /usr/local/bin/bestteam-health-check.sh    # 不用等下一个 10 分钟，直接手动跑
sudo tail -n 20 /var/log/bestteam-health.log
docker compose start backend
```

期望：日志里出现一段 `=== <时间> check-health exit=1`，后面跟着 Docker 的报错（措辞随版本变，大意是 `service "backend" is not running`）。**看到这一段，才算这条告警链路验证过。**

**测试 B（可选，但更接近真实故障）：进程活着，轮询停摆**

需要 `drilltest` 的自动运行还开着（也就是演练 3 §4.8 之后的状态）：

```bash
docker compose stop backend
# 等 7 分钟以上——阈值是 max(3 × 120 秒, 300 秒) = 360 秒
docker compose start backend
# 启动完立刻跑，别等
docker compose exec backend python -m ui.backend.admin check-health; echo "exit=$?"
```

期望：`[FAIL] poll[drilltest]: last mailbox check was 4xxs ago (interval 120s) -- the poller looks stalled or the process is down`，`exit=1`。

再等两三分钟重跑一次，应该恢复成 `[OK] poll[drilltest]: checked ...s ago`。**为什么不是一启动就变绿**：轮询循环是**先睡一个间隔再检查**（`poll_forever`，避免启动瞬间打一次真实邮箱），所以启动后要过一轮才有新的检查时间。

#### 5.1.6 一个必须知道的盲区

**没有任何 org 开着自动运行时，`check-health` 只打印一行然后退出 0**：

```
[OK]   triggers: no org has automatic email runs enabled
no failures, 0 warning(s)
```

也就是说：从你清理掉演练数据（5.5）到第一个真实客户打开自动运行之间，这条 cron **永远是绿的，但它其实什么都没在看**。别把这段时间的「一直没报警」当成「监控已经验证过了」——所以 5.1.5 那次人为失败**一定要趁演练数据还在、自动运行还开着的时候做完**。

另外：`/var/log/bestteam-health.log` 只在失败时写入，长不起来，可以不管；5.2 那个备份日志是每天都写的，交给 `logrotate` 或者定期自己截断。

### 5.2 确认备份 cron 也已经配好

不属于这次演练，但属于同一批收尾动作，而且是最容易漏的一件——演练 2 只证明了「`restore.sh` 能用」，没有证明「明天早上真的会有一份备份可以拿来 restore」。

按 `docs/deployment.md`「Backup and restore」配置（路径按你的实际情况改）：

```bash
sudo mkdir -p /var/backups/bestteam
sudo crontab -e
```

> **这次打开的 crontab 不是空的**——5.1.4 加的那两行还在里面。下面这一行是**追加在它们下面**的，不是替换掉整个文件（把原来的内容删掉，巡检 cron 就一起没了）。`MAILTO=""` 对整个 crontab 生效，备份这一行也被它管住，不用再写一遍。

```
15 3 * * * cd /opt/bestteam && ./scripts/backup-db.sh /var/backups/bestteam/bestteam-$(date +\%F).db >> /var/log/bestteam-backup.log 2>&1 && ./scripts/backup-files.sh /var/backups/bestteam/bestteam-files-$(date +\%F).tgz >> /var/log/bestteam-backup.log 2>&1
```

这一行里有三个不能改坏的地方：

- **`cd /opt/bestteam &&` 不能省**。两个脚本内部都直接调 `docker compose`（没带 `-f`），靠当前目录找到 `docker-compose.yml`。
- **`\%F` 的反斜杠不能省**（见 5.1.4 的 `%` 说明），去掉之后命令会被从那里截断。
- **`&&` 是串联**：数据库备份失败的话，文件备份根本不会跑。这是有意的（数据库才是主体），但意味着**失败是静默的**——只有 `/var/log/bestteam-backup.log` 里有痕迹，没有人会主动去看。

所以第二天早上必须**手动确认一次**，这条 cron 才算验证过：

```bash
ls -la /var/backups/bestteam/
tail -n 20 /var/log/bestteam-backup.log
```

期望：当天日期的 `.db` 和 `.tgz` 各一份，日志里是两行 `Backed up ... to ...`。

> **`.tgz` 很小甚至只有几百字节是正常的**，不是备份失败。它装的是数据卷上**除数据库以外**的东西——知识库上传的原始文档、builder 会话的工作区。没上传过知识库文档的部署，这个包本来就几乎是空的。数据库那份 `.db` 才是主体。
>
> 另外：**这两个脚本不带参数时，默认写到当前目录下的 `backups/`**（也就是 `/opt/bestteam/backups/`），不是写到你以为的地方。手动跑的时候如果找不到文件，先去那儿看看。

最后两件事，缺一个这份备份就是假的：

- **清理旧备份**，否则磁盘迟早满。同一个 crontab 里加一行最简单：
  ```
  30 4 * * * find /var/backups/bestteam -mtime +30 -delete
  ```
- **把备份复制到这台机器之外**（对象存储、另一台机器、你自己的电脑都行）。**和数据库一起坏掉的备份不是备份**——磁盘故障、误删整个目录、VPS 被回收，这三种情况下同机备份全都救不了你。

### 5.3 确认 `BESTTEAM_SECRETS_KEY` 已经安全备份

演练 3 里连接 M365 邮箱用到的 client secret，是用 `BESTTEAM_SECRETS_KEY`（Fernet）加密后存进数据库的。**这个 key 丢了，数据库备份对邮件功能就是废的。**

> 注意有两个长得几乎一样的变量：`BESTTEAM_SECRET_KEY`（会话签名）和 `BESTTEAM_SECRETS_KEY`（凭据加密），差一个 `S`。两者填成同一个值时 backend 直接拒绝启动，`check-env` 会报 FAIL。

要确认的不是「我存过了」，而是**「我存的那一份能用」**。真去对一次：

```bash
grep BESTTEAM_SECRETS_KEY /opt/bestteam/.env
```

把这一行和密码管理器里的那份**逐字比对**——尤其是结尾的 `=`，Fernet key 是 base64，末尾常有一到两个等号，手抄很容易漏掉。

同时确认：

- 它保存在密码管理器 / 密钥保管工具里，**不和数据库备份放在一起**（放一起等于加密白做：偷到备份的人连钥匙一起拿走了）
- 这台服务器丢失、需要重建时，你知道去哪里找回它

**丢了会发生什么**（知道后果，才知道这件事值不值得认真做）：backend 会**直接拒绝启动**，并且点名是哪几个 org 的凭据解不开：

```
BESTTEAM_SECRETS_KEY cannot decrypt the stored email credentials for org id(s) [3]
(wrong or rotated key). Restore the original key, or clear and re-enter the
affected mailboxes ...
```

这是刻意设计的——让问题在启动时就暴露，而不是等到某个客户的邮件跑到一半才失败（`ui/backend/db/email_credentials.py:ensure_secrets_key_for_stored_credentials`）。**此时 operator CLI 仍然能跑**，补救办法是清掉再重新录入受影响的邮箱：

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin clear-email <org>
docker compose run --rm --no-deps backend python -m ui.backend.admin set-email <org> --user ... --test
```

**没有原地换钥匙的命令**——换 key 就意味着挨个 org 重新录一遍邮箱凭据，也就意味着要去找每个客户重新拿一次 client secret。所以这个 key 的正确做法是**存好、别换**。

### 5.4 给真实客户设置留存期

正式客户上线前，决定这个 org 的邮件历史要保留多久。**默认是永久保留**，而且升级软件永远不会替客户改这个设置。

**这是在前端操作的，没有对应的 CLI 命令**——`PUT /api/org/retention` 走的是 org 成员登录。用该 org 的账号登录，进 **Activity → Data**（页面标题 "Your data"）：

1. **先点 "Download export"**，把「将来会被删掉的那些内容」下载一份 JSON 存档。删掉之后拿不回来，这一步只花几秒钟。
2. **How long to keep run history** 里选 30 / 90 / 180 / 365 天，或者保持 **Keep forever**（客户明确要求时）。
3. 点 **Save**。**保存本身不删任何东西**，删除发生在下一次清理。
4. 等两三分钟刷新页面，应该出现一行 `Last cleanup: <时间>, removed N run(s)`（N 通常是 0）。**这一行就是「清理确实在跑」的证据。** 清理挂在邮件轮询的定时器上，默认 120 秒一轮，**即使这个 org 没开自动运行也照跑**（`email_trigger.run_maintenance`）。
5. 页面下方还有 **Remove history now**：需要手打 `DELETE` 确认，作用是不等下一轮清理、立刻删掉比留存期更老的已完成 run 的内容。演练里用不到，但要知道它在那儿。

**清理删什么、留什么**（跟客户解释时要说准）：删的是**内容**——邮件正文、我们写的草稿、逐步 trace；留的是**账目**——这个 run 跑过、什么时候跑的、花了多少、哪些邮件已经回过。留账目不是打折，是为了计费和「别对同一封邮件回两次」还能成立。

设置完之后跑一遍 `check-env` 确认当前状态：

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
```

它会**点名**还在用永久保留的 org：

```
[WARN] org retention: org(s) keeping run history forever: drilltest. Set a
retention period per org (PUT /api/org/retention) before a real customer uses it
```

注意这是 **WARN 不是 FAIL**，不影响退出码——它提醒你，但不会拦着你上线。

### 5.5 清理这次演练留下的测试数据

演练全部通过之后再做这一步（前面几节还要用到 `drilltest`）。**按顺序执行**：

```bash
# 1. 先把演练用的邮箱凭据从数据库里清掉
docker compose exec backend python -m ui.backend.admin clear-email drilltest

# 2. 删掉测试账号
docker compose exec backend python -m ui.backend.admin delete-user drilluser

# 3. 停用这两个 org（见下面的说明——这一步很重要）
docker compose exec backend python -m ui.backend.admin deactivate-org drilltest
docker compose exec backend python -m ui.backend.admin deactivate-org afterbackup   # 如果它还在

# 4. 确认最终状态
docker compose exec backend python -m ui.backend.admin list-orgs
```

**第 1 步为什么要单独做**：`delete-user` 删的是账号，不是 org 的邮箱凭据。不 `clear-email` 的话，演练用的那份 client secret 会一直躺在数据库里（虽然是加密的，而且你在 §4.11 已经把 Azure 那边删掉、它早就失效了）。清掉更干净。

**第 3 步为什么重要**：`deactivate-org` 会让轮询器和 `check-health` **同时**跳过这个 org（两边用的是同一个过滤条件：trigger 开启 **且** org 处于 active）。不停用的话，一个没有成员、凭据已失效的 org 会被永远轮询下去，每两分钟失败一次。这不会弄坏别人，但会在日志里刷噪音。

**关于 `drilltest` / `afterbackup` 这两个 org 本身**：目前**没有 `delete-org` 命令**，`deactivate-org` 是能做到的最干净的状态。一个停用的 org 不出现在任何客户可见的地方，也不产生费用。唯一的残留是 5.4 那条 `check-env` 的 WARN——**它不看 org 是否 active，所以这两个 org 会永远出现在「还在永久保留」的名单里**。两个选择：

- **接受它**，并在签署清单上写清楚「名单里的 `drilltest` / `afterbackup` 是演练残留，不是漏配的客户」。否则三个月后你自己看到那条 WARN 也要重新查一遍。
- **恢复到演练之前**：如果你在演练最开始（第 1 节之前）做过一次备份，直接 `restore.sh` 回去最干净，一点痕迹都不留。没做过就选上一条，不值得为此重建部署。

演练 2 用的 `afterbackup` 正常情况下已经被 §3.4 的恢复步骤本身抹掉了——`list-orgs` 里看不到它是预期结果。只有在你跑 `restore.sh` 之前中止了演练时它才会留下来。

### 5.6 最终签署清单

全部完成后，把下面这份清单填一遍，作为「这台部署已经可以接真实客户」的证据留存（贴进工单系统、Slack、或者随便一个能追溯的地方都可以）：

- [ ] 演练 1（强杀/重启/恢复）全部通过标准打勾
- [ ] 演练 2（备份/恢复）全部通过标准打勾
- [ ] 演练 3（M365）全部通过标准打勾（如果客户不用 M365，这一项可以跳过，但要在清单上明确写「客户邮箱类型：非 M365，跳过」）
- [ ] `check-health` 已接入 cron，并且**用 5.1.5 的方法人为制造过一次失败、确认日志里真的出现了告警**（只确认「配好了」不算）
- [ ] 知道 5.1.6 那个盲区：没有 org 开自动运行时，这个巡检恒绿
- [ ] 备份 cron 已配置，且**第二天早上亲眼确认过 `.db` 和 `.tgz` 都生成了**
- [ ] 备份有异地副本，且配了旧文件清理
- [ ] `BESTTEAM_SECRETS_KEY` 已安全备份，并和 `.env` 里的值**逐字对比过**
- [ ] 已决定并设置了这个客户 org 的留存期（或明确记录「客户要求永久保留」）
- [ ] 本次演练用的测试账号已清理，测试 org 已停用
- [ ] 演练日期、执行人、遇到的问题（如果有）已记录
