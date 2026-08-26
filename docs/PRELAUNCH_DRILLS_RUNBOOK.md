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

**在哪里做**：必须在**目标 VPS** 上做（不能在本地开发机上，因为本地机器大概率没有 Docker，而且这份演练本身就是在验证「部署环境」）。全程使用一个**测试用的 org/用户**，不要用真实客户的数据——即便这台 VPS 就是将来给客户用的那台也没关系，只要还没有真实客户上线，跑完演练后把测试数据清掉即可（第 4.4 节有清理步骤）。

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
2. 登录前端（`https://<你的域名>` 或 `http://<VPS-IP>:5173`，取决于你怎么部署），用 `drilluser` 登录，走 Team Builder 向导部署一个用到邮件工具的团队，并在 Deploy 页打开「自动运行」。
3. 给测试邮箱发一封新邮件。

### 2.2 强杀 backend

**记录下 kill 之前的状态**，方便对比：

```bash
docker compose exec backend python -m ui.backend.admin check-health
```

然后模拟一次「毫无征兆的崩溃」——不是优雅停止，是直接杀掉进程（`docker compose kill` 发的是 `SIGKILL`，跳过任何清理逻辑，这正是我们要测的最坏情况）：

```bash
docker compose kill -s SIGKILL backend
docker compose ps   # backend 应该显示 Exited
```

### 2.3 重新拉起来，检查恢复情况

```bash
docker compose up -d backend
```

等它启动后（几秒钟），依次确认：

| 检查项 | 命令 | 期望结果 |
|---|---|---|
| 服务恢复健康 | `curl http://localhost:8000/api/health` | `200 {"status":"ok",...}` |
| 启动日志里能看到清理动作 | `docker compose logs backend --since 5m \| grep -i "sweep\|interrupted\|orphan"` | 能看到类似 "swept N interrupted run(s)" 的字样（如果 kill 时刚好没有 running 的 run，可能是 0 条，这也正常——说明没有东西需要清理） |
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

### 3.1 先产生一些"看得出有没有恢复成功"的数据

演练 1 里创建的 `drilltest` org 和 `drilluser` 正好可以用。为了能明确判断"恢复成功了"，再加一个之后会删除的标记：

```bash
docker compose exec backend python -m ui.backend.admin create-user restoremarker --org drilltest
# 密码随便设，记下来——这个账号的存在与否就是"恢复是否成功"的证据
```

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
docker compose exec backend python -m ui.backend.admin create-user afterbackup --org drilltest
```

现在 `afterbackup` 这个账号存在，但它是在备份**之后**创建的——恢复完成后，这个账号应该**消失**（因为恢复回到了备份那一刻，备份之后发生的事情不存在）。

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
| 备份时已存在的账号还在 | 用 `restoremarker` 的账号密码登录 | 能登录成功 |
| 备份之后才创建的账号消失了 | 用 `afterbackup` 的账号密码登录 | 登录失败（用户不存在）——**这一步是恢复真的生效的关键证据**，如果这个账号还在，说明恢复根本没有真的替换数据库 |
| 数据文件也恢复了（如果测试时上传过知识库文档） | 检查对应的知识库文档还在 | 文档存在，可以正常检索 |

### 3.6 记录这次演练特有的两个未知点

`project_beta_freeze` 里提到，有两件事只有真正跑一次 `restore.sh` 才能确认，请在这次演练里顺手记录下来（不需要额外操作，只是留意结果）：

1. **`docker compose cp` 写入一个已停止的容器，是否真的写穿到了数据卷** —— 3.5 节如果验证通过，就说明写穿了，没问题；如果验证失败但脚本本身没报错，很可能是这个环节出了问题，需要进一步排查。
2. **文件恢复是"追加"而不是"替换"** —— `tar xzf` 没有删除语义，所以恢复文件归档之后，**备份之后新建的知识库上传目录不会被清理，会变成孤儿目录**（数据库已经回滚，不再引用它们）。这不是 bug，只是要知道：`restore.sh` 恢复完之后，如果想要一个"干净"的状态，可能需要手动清理这些孤儿目录。可以顺手确认一下：`afterbackup` 账号存在期间如果上传过知识库文档，恢复后对应的文件目录是否还残留在磁盘上。

### 3.7 通过标准

- [ ] `restore.sh` 跑完打印 "Restore complete: the backend is healthy."
- [ ] 备份时已存在的账号能登录
- [ ] 备份之后创建的账号无法登录（证明真的回滚了，不是假通过）
- [ ] 记录了上面两个未知点的实际观察结果

---

## 4. 演练 3：M365 真实租户 smoke test + 密钥轮换 + 权限撤销

**验证的问题**：如果客户的邮箱是 Microsoft 365（工作/学校账号），代码路径和 Azure 官方文档已经核对过是一致的，但**从来没有真正连过一个真实的 Azure 租户**。这个演练同时也是在把"给 M365 客户接入邮箱"这件事走一遍，走完之后你自己就有了一份可以照做的操作经验——这就是欠账里说的 "onboarding runbook"。

**前提**：你需要能访问一个 Microsoft 365 **工作或学校**租户（不能是个人 Hotmail/Outlook.com 账号，那个走不通，见 `docs/email-smoke-test.md` §15）并且有该租户的**全局管理员**或者能被临时授予相应权限的账号——Entra 应用注册、授予应用权限、Exchange Online PowerShell 操作都需要管理员权限。如果暂时没有这样的测试租户，这个演练需要先申请一个（很多组织都有 Microsoft 365 开发者订阅可以免费申请测试租户）。

预计耗时：Azure 那一侧的设置和等待权限生效大约 30–40 分钟，之后的连接测试大约 10 分钟。

### 4.1 在 Azure 里注册应用（这一步是客户 IT 未来要做的事，你现在先自己走一遍）

按 `docs/deployment.md` →「Microsoft 365 mailboxes」的四步操作，在测试租户里：

1. **Entra ID → App registrations → New registration**，注册一个应用。记下：
   - **Directory (tenant) ID**
   - **Application (client) ID**
   - 创建一个 **client secret**（创建的瞬间把值复制下来，之后就再也看不到明文了；同时记下 Azure 显示的**过期日期**，等下会用到）
2. **API permissions → Add a permission → APIs my organization uses → Office 365 Exchange Online → Application permissions → `IMAP.AccessAsApp`**，添加后点 **Grant admin consent**。
3. 打开 **Exchange Online PowerShell**（`Connect-ExchangeOnline`），执行：
   ```powershell
   New-ServicePrincipal -AppId <application-client-id> -ServiceId <object-id>
   Add-MailboxPermission -Identity <测试邮箱地址> -User <object-id> -AccessRights FullAccess
   ```
   `<object-id>` 是这个应用在 Entra 里的对象 ID（App registration 概览页能看到）。
4. **建议同时做**：给这个应用配置一个 Exchange **Application Access Policy**，把它限制到只能访问这一个测试邮箱——这样即便密钥泄露，影响范围也只有一个邮箱，不会波及租户里其他人的邮件。

> 权限授予之后可能需要几分钟到十几分钟才会在 Exchange 侧生效，如果下一步连接失败，先等几分钟再试。

### 4.2 用这几个值连接（分场景）

**场景 A：走操作员 CLI（如果你是替客户配置）**

```bash
docker compose exec backend python -m ui.backend.admin set-email drilltest \
  --auth microsoft-oauth --user <测试邮箱地址> \
  --tenant <directory-tenant-id> --client-id <application-client-id> --test
```
会提示你输入 client secret（不会显示在屏幕上，也不会留在 shell 历史里）。`--test` 会在保存前先做一次真实连接验证。

**场景 B：走客户自助向导（体验客户会看到的流程）**

用 `drilluser` 登录前端，走到 Team Builder 的「Connect your mailbox」步骤，选择 **Microsoft 365 / Outlook (Exchange Online)**，依次填入邮箱地址、Directory (tenant) ID、Application (client) ID、client secret，点 **Test connection**。

两种场景都建议走一遍，因为客户大概率是场景 B，但操作员补救/代配置时用的是场景 A。

### 4.3 确认每一种失败都有"看得懂的提示"

`docs/email-smoke-test.md` §9.3 列了四种典型的配置错误，这是最值得花时间验证的部分——因为客户自己遇到这些错误时，能不能看懂提示直接决定了他们会不会来找你求助。依次**故意改错一个值**，确认报错内容对得上：

| 改错哪个值 | 应该看到的提示应该提到 |
|---|---|
| client secret 改成错的 | Application (client) ID / client secret |
| tenant ID 改成错的 | Directory (tenant) ID |
| 凭据都对，但跳过 4.1 第 3 步的 `Add-MailboxPermission` | `IMAP.AccessAsApp`、`New-ServicePrincipal`、`Add-MailboxPermission` |
| 邮箱地址填一个不在这个租户里的地址 | 同样是"访问被拒绝"类提示，并且提示里点名了这个邮箱地址 |

**第三行最值得注意**：一个"token 能拿到、但邮箱访问被拒绝"的情况，和"密钥填错了"从错误信息本身几乎无法区分，除非系统明确区分了这两个失败阶段。如果测试下来这一行的提示和"密钥填错"的提示看起来一样含糊，这是需要上报的问题。

### 4.4 端到端跑一次，并开启自动轮询

把 4.2 连上的邮箱接一个真实团队并跑一次：

1. 给测试邮箱发一封新邮件。
2. 用这个邮箱运行邮件团队（手动运行一次），确认 Drafts 文件夹里出现了回复草稿，Sent 文件夹里**没有**任何东西被发出（这是核心安全属性：只生成草稿，绝不自动发送）。
3. 在 Deploy 页打开「自动运行」，再发一封新邮件，确认几分钟内（取决于轮询间隔）自动出现了新的草稿，不需要人工点运行。
4. **放着跑超过 1 小时**，确认 token 刷新正常——`docs/email-smoke-test.md` §9.4 特别提到：access token 大约 1 小时过期，会在过期前 60 秒自动刷新；如果卡在 1 小时这个点，说明刷新没有正常工作。

### 4.5 密钥轮换演练

模拟"这个 client secret 快过期了，需要换一把新的"这个真实会发生的场景（Azure client secret 通常 6–24 个月过期，**这一定会在客户的生命周期里发生**）：

1. 回到 Entra 应用注册页，为同一个应用**再创建一个新的 client secret**（不要先删旧的——先加新的，验证完再删旧的，这样中途不会出现"两边都不能用"的空窗期）。
2. 用新 secret 重新执行场景 A 或场景 B 的连接步骤（`set-email --test` 或向导里重新填写）。
3. 确认连接测试通过，自动轮询在下一个周期恢复正常（如果之前旧密钥还没过期，轮询本来就没中断；如果为了测试，先删掉旧密钥制造一次"密钥失效"的状态，再走轮换，更接近真实场景，见下一步）。
4. **可选但推荐**：先删除旧密钥（制造真实的失效），确认几个轮询周期内该 org 收到了"Microsoft 365 client secret nearing expiry / 无法连接"类的告警通知（Activity → Alerts），再补上新密钥验证恢复——这样能顺便验证密钥过期告警链路本身是通的。
5. 在 Azure 里删除旧的 client secret，完成轮换。

**记录下来**：从"决定轮换"到"轮询恢复正常"，实际花了多长时间、需要几步操作——这就是将来写给客户/自己看的轮换 SOP 的素材。

### 4.6 权限撤销演练

模拟客户方 IT 收回授权这件事（比如客户离职、审计要求、或者客户决定不再使用这个产品）：

1. 在 Exchange Online PowerShell 里撤销刚才授予的邮箱权限：
   ```powershell
   Remove-MailboxPermission -Identity <测试邮箱地址> -User <object-id> -AccessRights FullAccess
   ```
   或者更彻底地，撤销 `IMAP.AccessAsApp` 的 admin consent（Entra ID → Enterprise applications → 找到这个应用 → Permissions → Revoke），或者直接删除整个 App registration。
2. 等下一个轮询周期，确认：
   - 该 org 在 Activity → Alerts 里出现了"邮箱无法访问"类的告警（"Your mailbox can't be reached"）
   - 系统**没有**因为这个失败而崩溃或影响其他 org
   - 手动触发一次连接测试（`set-email --test`，或向导里的 Test connection），错误提示能让人明白是权限被收回了，而不是一个模糊的报错

### 4.7 清理

演练做完后，在 Azure 里删除这个测试用的 App registration（如果还没删的话），避免留下一个没人管理但仍然存在的应用注册。

### 4.8 通过标准

- [ ] 4.1 的四步 Azure 设置全部走完，能看到每一步实际的操作界面/命令（不是照抄文档、心里觉得"应该没问题"）
- [ ] 4.3 的四种错误配置，每一种的报错提示都对得上表格里描述的内容
- [ ] 端到端能收到真实草稿，Sent 里确认没有任何邮件被发出
- [ ] 自动轮询跑过 1 小时以上没有中断（token 刷新正常）
- [ ] 完整走过一次密钥轮换，且记录了实际耗时
- [ ] 权限撤销后，告警在预期时间内出现，系统没有异常
- [ ] 把 4.1–4.6 的实际步骤、耗时、遇到的坑整理成一份给自己/客户 IT 看的简版操作单（这就是欠账里说的 onboarding runbook——不需要另外写文档，把这次演练的真实记录整理一下就是它）

---

## 5. 收尾：check-health 上线 + 设置留存期 + 清理测试数据

三件演练之外、但同样是"上线前必须做"的收尾动作。

### 5.1 把 check-health 接入 cron

演练过程中你已经手动跑过几次 `check-health`，现在把它变成自动巡检：

```bash
crontab -e
```

加一行（`docs/deployment.md` "Watching the watcher" 里的示例，按你的部署目录调整路径）：

```
*/10 * * * * docker compose -f /opt/bestteam/docker-compose.yml exec -T backend python -m ui.backend.admin check-health || echo "bestteam check-health FAILED at $(date)" >> /var/log/bestteam-health.log
```

`|| ...` 后面接你实际想要的告警方式（发邮件给自己、写日志配合日志监控工具、调用一个 webhook 都行——这里只是给一个最小可用的兜底，不需要一开始就做得很复杂）。

验证 cron 真的会跑：等 10 分钟后检查 `/var/log/bestteam-health.log`（如果一切正常，这个文件应该不会出现新内容，因为只有失败才写日志——可以先临时把 crontab 里的时间改成每分钟测试一次，确认没有报错后再改回 10 分钟）。

同时确认**备份 cron** 也已经按 `docs/deployment.md`「Backup and restore」配置好了（不是这次演练的一部分，但这是同一批收尾工作，容易漏掉）：

```
15 3 * * * cd /opt/bestteam && ./scripts/backup-db.sh /var/backups/bestteam/bestteam-$(date +\%F).db >> /var/log/bestteam-backup.log 2>&1 && ./scripts/backup-files.sh /var/backups/bestteam/bestteam-files-$(date +\%F).tgz >> /var/log/bestteam-backup.log 2>&1
```

### 5.2 确认 `BESTTEAM_SECRETS_KEY` 已经安全备份

第 4 节里连接 M365 邮箱用到的 client secret，是用 `BESTTEAM_SECRETS_KEY` 加密存进数据库的。确认这个 key：

- 已经保存在密码管理器/密钥保管工具里（**不要**和数据库备份放在一起——两者放一起等于加密白做）
- 在这台服务器丢失/需要重建时，你知道去哪里找回它

### 5.3 给真实客户设置留存期

正式客户上线前，决定这个 org 要不要设一个有限的邮件历史留存期（默认是永久保留）。这是在前端操作的：

**Activity → Data** 标签页，选择 30/90/180/365 天，或者继续保持"永久保留"（如果客户明确要求）。

如果不确定客户的要求，`check-env` 现在会点名哪些已存在的 org 还在用"永久保留"（see `project_aug24_reassessment` 里 PR #92 加的这项检查），可以先跑一遍确认当前状态：

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
```

### 5.4 清理这次演练留下的测试数据

演练全部通过之后，清掉本次用到的测试账号，避免它们和将来真实客户的数据混在一起：

```bash
docker compose exec backend python -m ui.backend.admin delete-user drilluser
docker compose exec backend python -m ui.backend.admin delete-user restoremarker
docker compose exec backend python -m ui.backend.admin delete-user afterbackup   # 如果演练 2 之后它还在（正常情况下恢复演练本身应该已经让它消失了）
```

`drilltest` 这个 org 本身没有单独的删除命令（`admin.py` 目前没有 `delete-org`），删完成员账号即可——一个没有成员的 org 不会出现在任何客户可见的地方，也不会产生费用。如果你想彻底清掉，可以在这次演练全部做完、确认没有问题之后，直接从头 `restore.sh` 一份"演练开始前"状态的备份（如果你在演练最开始也做过一次备份的话），这样最干净。

### 5.5 最终签署清单

全部完成后，把下面这份清单填一遍，作为"这台部署已经可以接真实客户"的证据留存（贴进工单系统、Slack、或者随便一个能追溯的地方都可以）：

- [ ] 演练 1（强杀/重启/恢复）全部通过标准打勾
- [ ] 演练 2（备份/恢复）全部通过标准打勾
- [ ] 演练 3（M365）全部通过标准打勾（如果客户不用 M365，这一项可以跳过，但要在清单上明确写"客户邮箱类型：非 M365，跳过"）
- [ ] `check-health` 已接入 cron 并验证过会正常报错
- [ ] 备份 cron 已配置
- [ ] `BESTTEAM_SECRETS_KEY` 已安全备份
- [ ] 已决定并设置了这个客户 org 的留存期
- [ ] 本次演练用的测试账号已清理
- [ ] 演练日期、执行人、遇到的问题（如果有）已记录
