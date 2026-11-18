"""切换登录账号 —— 备份 → 关客户端 → 搬数据 → 写认证 → 拉起。

## 为什么这一套是可能的（推翻之前的结论）

早期结论是「客户端不支持外部切换账号，只能重新登录」，依据是 asar 里
`switchAccount()` 走 `/v2/login/enterprise`（那是重新登录）以及
`file-authentication-storage` 只存当前这一份 session。

**漏掉的是**：那份 session 就是一个明文 JSON —— `workbuddy-desktop.info`，
四个顶层键 `{account, auth, accounts, allAccounts}`。既然它是文件，就能被写；
既然另一份账号的 `{account, auth}` 能被存下来（vault），就能被写回去。
客户端启动时读它、用它 —— 身份就是这么定的，不需要任何网络交互。

所以本模块干的事很朴素：**换掉那个文件里的两段 JSON**。剩下的活都是保险。

## 顺序即语义（每一步失败意味着什么）

    1. 采集当前账号  → 失败可视作无事发生（下次启动还会再采一次）
    2. 备份认证文件  → 失败**中止**：这是唯一能把登录态退回原状的东西
    3. 关闭客户端    → 失败**中止**：认证文件是客户端启动时读、运行中也可能回写，
                        热写会让两边打架，结果不可预测
    4. 复制会话      → 有硬失败**中止**：数据没搬成却切了身份，用户会以为"什么都没有"
    5. 补齐人格      → 失败只警告：只补目标账号缺失的文件，已存在的一概不动
    6. 合并记忆      → 失败只警告：记忆是锦上添花，不该阻断身份切换
    7. 写认证文件    → 失败则回滚（把备份写回去）并重启，用户还是原来的账号
    8. 拉起客户端    → 失败只报告：身份已经切好了，只是没自动开

## 为什么人格文件要单列一步（而设置项不用）

人格文件（SOUL / IDENTITY / USER / BOOTSTRAP）在 `user-<uid>-personal/` 下，
**按账号隔离**：切到新 uid 后客户端读的是那个账号自己的目录，缺文件就是空人格。
这是"切过去感觉设置没跟着走"的真正来源之一。

设置项 / 技能 / 界面偏好则存在 `~/.workbuddy` 根下，是这台机器共用的一份，
切换账号前后是同一份文件，搬它等于把文件写回它自己 —— 因此不列迁移项。
工作空间记忆在 `<项目目录>/.workbuddy/memory/`，跟项目目录走，与账号无关，同样不列。

人格补齐沿用账号对齐既有的"只补不覆盖"语义，并直接复用 `align.persona_dir_of`
的定位逻辑（多账号机器上 `config_items.persona_dir()` 只返回第一个，会拿错人）。
它只写入目标不存在的文件，天然不覆盖用户内容，因此无需备份。

## 与导入流程共用同一条铁律

客户端运行中一律不写库、不写认证文件。这条不因为「只改一个 json 文件」而放宽 ——
客户端在退出时会把内存里的登录态**回写**到同一个文件，谁先谁后不可控。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import accounts, align, authfile, closeflow, config_items, copysession, memorymerge, vault


@dataclass
class PersonaReport:
    """人格文件补齐结果。只补目标缺失的文件，不覆盖任何已存在的。"""

    added: list[str] = field(default_factory=list)       # 实际补入的文件名
    conflicts: list[str] = field(default_factory=list)   # 目标已有且内容不同，未覆盖
    same: list[str] = field(default_factory=list)        # 两端一致，无需动作
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added)

    def summary(self) -> str:
        if not self.changed:
            return "人格文件无需补齐"
        parts = [f"补入 {len(self.added)} 个人格文件"]
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} 个目标已有、未覆盖")
        return "，".join(parts)


def _persona_files(uid: str, base) -> dict[str, bytes]:
    """读一个账号的人格文件。目录定位复用 align，避免取错账号。"""
    d = align.persona_dir_of(uid, base)
    out: dict[str, bytes] = {}
    for name, _label in config_items.PERSONA_FILES:
        p = d / name
        if p.is_file():
            try:
                out[name] = p.read_bytes()
            except OSError:
                continue
    return out


def preview_persona(source_uid: str, target_uid: str, base) -> tuple[list[str], list[str]]:
    """只读预览：返回 (待补文件名, 目标已有且不同)。"""
    if not source_uid or not target_uid or source_uid == target_uid:
        return [], []
    src = _persona_files(source_uid, base)
    if not src:
        return [], []
    dst = _persona_files(target_uid, base)
    add = [name for name in src if name not in dst]
    conflict = [name for name in src if name in dst and dst[name] != src[name]]
    return add, conflict


def execute_persona(source_uid: str, target_uid: str, base) -> PersonaReport:
    """把源账号的人格文件补进目标账号：只补缺失，已存在的一概不覆盖。"""
    report = PersonaReport()
    if not source_uid or not target_uid or source_uid == target_uid:
        report.warnings.append("源账号与目标账号相同，或缺少账号，未补人格文件")
        return report
    src = _persona_files(source_uid, base)
    if not src:
        report.warnings.append("源账号没有人格文件，跳过")
        return report
    dst = _persona_files(target_uid, base)
    add = [name for name in src if name not in dst]
    report.conflicts = [name for name in src if name in dst and dst[name] != src[name]]
    report.same = [name for name in src if name in dst and dst[name] == src[name]]
    if not add:
        return report

    dst_dir = align.persona_dir_of(target_uid, base)
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.warnings.append(f"创建目标人格目录失败（{exc.strerror or exc}），未补人格文件")
        return report
    for name in add:
        target = dst_dir / name
        # 再查一次：扫描到写入之间目标可能被客户端补上了，此时不覆盖
        if target.exists():
            report.warnings.append(f"{name} 在写入前已出现，跳过以免覆盖")
            continue
        try:
            tmp = target.with_name(target.name + ".wbsync-tmp")
            tmp.write_bytes(src[name])
            os.replace(tmp, target)
            report.added.append(name)
        except OSError as exc:
            report.warnings.append(f"补 {name} 失败：{exc.strerror or exc}")
    return report


@dataclass
class SwitchPlan:
    """切换计划。`plan_switch` 只读，不写任何东西。"""

    target_uid: str = ""
    target_name: str = ""
    current_uid: str = ""
    current_name: str = ""
    credential: vault.Credential | None = None
    copy_plan: copysession.CopyPlan | None = None
    do_persona: bool = False
    persona_add: list[str] = field(default_factory=list)       # 待补的人格文件名
    persona_conflict: list[str] = field(default_factory=list)  # 目标已有且不同，不覆盖
    do_memory: bool = False
    memory_preview: memorymerge.MergeReport | None = None
    # 阻止执行的原因（空串 = 可以执行）
    block_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    restart: bool = True

    @property
    def ready(self) -> bool:
        return not self.block_reason

    @property
    def same_account(self) -> bool:
        return bool(self.current_uid) and self.current_uid == self.target_uid

    def steps(self) -> list[str]:
        """给确认页用的人话步骤列表。"""
        out = [f"关闭 WorkBuddy（当前登录：{self.current_name or '未识别'}）"]
        if self.copy_plan is not None and self.copy_plan.copyable:
            out.append(f"复制 {len(self.copy_plan.copyable)} 条会话到「{self.target_name}」")
        if self.do_persona and self.persona_add:
            out.append(f"补齐 {len(self.persona_add)} 个人格文件到「{self.target_name}」")
        if self.do_memory:
            out.append(f"合并长期记忆到「{self.target_name}」")
        out.append(f"把登录身份写成「{self.target_name}」")
        out.append("重新启动 WorkBuddy")
        return out


@dataclass
class SwitchReport:
    """切换结果。"""

    ok: bool = False
    target_uid: str = ""
    target_name: str = ""
    current_uid: str = ""
    auth_backup: Path | None = None
    auth_written: bool = False
    copy_report: copysession.CopyReport | None = None
    persona_report: PersonaReport | None = None
    memory_report: memorymerge.MergeReport | None = None
    restarted: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return self.error or "切换未完成"
        parts = [f"已切换到「{self.target_name}」"]
        if self.copy_report is not None and self.copy_report.copied:
            parts.append(f"复制 {len(self.copy_report.copied)} 条会话")
        if self.persona_report is not None and self.persona_report.changed:
            parts.append(f"补齐 {len(self.persona_report.added)} 个人格文件")
        if self.memory_report is not None and self.memory_report.changed:
            parts.append("已合并长期记忆")
        if not self.restarted:
            parts.append("请手动启动 WorkBuddy")
        return "，".join(parts)


def plan_switch(
    target_uid: str,
    copy_cids: list[str] | None = None,
    do_persona: bool = False,
    do_memory: bool = False,
    base: str | Path | None = None,
    restart: bool = True,
    copy_all: bool = False,
) -> SwitchPlan:
    """算出一份切换计划。**只读**（人格与记忆合并只做 dry-run 预览）。

    `copy_cids` 非空 = 复制这些会话；`copy_all=True` = 复制源账号全部会话；
    两者都不给 = 不复制。
    """
    plan = SwitchPlan(
        target_uid=target_uid, restart=restart, do_persona=do_persona, do_memory=do_memory
    )
    labels = accounts.AccountLabels.load(base)
    plan.target_name = labels.label(target_uid) if target_uid else ""
    plan.current_uid = authfile.current_uid(base) or labels.primary_uid
    plan.current_name = labels.label(plan.current_uid) if plan.current_uid else ""

    if not target_uid:
        plan.block_reason = "未选择目标账号"
        return plan

    # 「目标就是当前账号」先判：这种情况不需要凭据，也不需要任何动作
    if plan.same_account:
        plan.block_reason = "目标账号就是当前登录账号，无需切换"
        return plan

    store = vault.Vault.load(base)
    plan.credential = store.get(target_uid)
    if plan.credential is None:
        plan.block_reason = (
            "本机没有该账号的登录凭据，无法切换。\n"
            "请先用该账号登录一次 WorkBuddy（本工具会自动采集），"
            "或从「其它工具的账号库」导入。"
        )
        return plan

    if copy_cids or copy_all:
        plan.copy_plan = copysession.plan_copy(
            target_uid=target_uid,
            source_uid=plan.current_uid,
            cids=None if copy_all else copy_cids,
            base=base,
        )
        plan.warnings.extend(plan.copy_plan.warnings)
        if plan.copy_plan.same_account:
            plan.copy_plan = None

    if do_persona:
        plan.persona_add, plan.persona_conflict = preview_persona(
            plan.current_uid, target_uid, base
        )
        if not plan.current_uid:
            plan.warnings.append("未识别当前登录账号，无法预览人格文件补齐")

    if do_memory:
        plan.memory_preview = memorymerge.merge(
            plan.current_uid, target_uid, base=base, dry_run=True
        )
        plan.warnings.extend(plan.memory_preview.warnings)

    if not authfile.auth_path(base).is_file():
        plan.block_reason = f"找不到认证文件：{authfile.auth_path(base)}"
    return plan


def execute_switch(
    plan: SwitchPlan,
    base: str | Path | None = None,
    progress=None,
) -> SwitchReport:
    """执行切换。会先关闭客户端；任一致命失败都返回 ok=False 并说明停在哪一步。"""
    report = SwitchReport(
        target_uid=plan.target_uid,
        target_name=plan.target_name,
        current_uid=plan.current_uid,
        warnings=list(plan.warnings),
    )
    if not plan.ready:
        report.error = plan.block_reason or "计划不可执行"
        return report

    # ---- 进度口径：先说「第几步 / 共几步」，再说这一步在做什么
    #
    # 原先只报「正在关闭 WorkBuddy…」，用户既不知道后面还有几件事，也不知道
    # 卡在了哪一件。步数按这份计划实际会做的事算，不写死 —— 不复制会话时
    # 报「3/7」而实际只有 6 步，比不报还糟。
    # 步数 = 固定四步（采集凭据 / 备份认证 / 关客户端 / 写身份）+ 可选四步
    total = 4 + (1 if plan.restart else 0)
    total += 1 if (plan.copy_plan is not None and plan.copy_plan.copyable) else 0
    total += 1 if (plan.do_persona and plan.persona_add) else 0
    total += 1 if plan.do_memory else 0
    step_no = 0

    def emit(text: str) -> None:
        if progress is not None:
            progress(text)

    def say(text: str) -> None:
        """推进到下一步。"""
        nonlocal step_no
        step_no = min(step_no + 1, total)
        emit(f"{step_no}/{total} {text}")

    def note(text: str) -> None:
        """同一步内的细粒度进度（复制第几条、等了几秒），不推进步号。"""
        emit(f"{step_no}/{total} {text}")

    # ---- 1. 采集当前账号（保证"还能切回去"）
    say("采集当前账号凭据…")
    store = vault.Vault.load(base)
    try:
        store.capture_current(base)
    except OSError as exc:
        report.warnings.append(f"当前账号凭据采集失败（不影响本次切换）：{exc}")

    cred = plan.credential or store.get(plan.target_uid)
    if cred is None:
        report.error = "目标账号凭据已丢失，请重新采集后再试"
        return report

    # ---- 2. 备份认证文件（唯一能退回原状的凭据）
    say("备份认证文件…")
    report.auth_backup = authfile.backup(base=base, label="auth")
    if report.auth_backup is None:
        report.error = "备份认证文件失败，已中止（这是唯一能退回原状的凭据）"
        return report

    # ---- 3. 关闭客户端
    say("关闭 WorkBuddy…")
    outcome = closeflow.prepare_import(
        progress=(
            (lambda used, left: note(f"等待 WorkBuddy 退出… {used:.0f}s，剩 {left} 个进程"))
            if progress
            else None
        )
    )
    if not outcome.ok:
        report.error = f"关闭 WorkBuddy 失败：{outcome.message}。已中止，数据与登录态均未改动。"
        return report

    try:
        # ---- 4. 复制会话（有硬失败就中止，别让用户切过去发现"什么都没有"）
        if plan.copy_plan is not None and plan.copy_plan.copyable:
            say(f"复制 {len(plan.copy_plan.copyable)} 条会话…")
            report.copy_report = copysession.execute_copy(
                plan.copy_plan,
                base=base,
                progress=(lambda done, total_, label: note(label)) if progress else None,
            )
            report.warnings.extend(report.copy_report.warnings)
            hard = [
                (cid, msg)
                for cid, msg in report.copy_report.failed
                if cid in ("workbuddy.db", "edge-sync-mapping")
            ]
            if hard:
                report.error = (
                    "会话复制失败，已在写入登录身份之前中止（你仍是原账号登录）。\n"
                    + "；".join(f"{m}" for _c, m in hard)
                )
                return report
            if not report.copy_report.copied and plan.copy_plan.copyable:
                report.warnings.append("没有会话被实际复制，其余步骤照常执行")

        # ---- 5. 补齐人格文件（只补缺失，失败只警告）
        if plan.do_persona and plan.persona_add:
            say(f"补齐 {len(plan.persona_add)} 个人格文件…")
            try:
                report.persona_report = execute_persona(
                    plan.current_uid, plan.target_uid, base
                )
                report.warnings.extend(report.persona_report.warnings)
            except (OSError, ValueError) as exc:
                report.warnings.append(f"人格文件补齐失败（已跳过）：{exc}")

        # ---- 6. 合并长期记忆（失败只警告）
        if plan.do_memory:
            say("合并长期记忆…")
            try:
                report.memory_report = memorymerge.merge(
                    plan.current_uid, plan.target_uid, base=base
                )
                report.warnings.extend(report.memory_report.warnings)
            except (OSError, ValueError) as exc:
                report.warnings.append(f"记忆合并失败（已跳过）：{exc}")

        # ---- 7. 写认证文件
        say("写入登录身份…")
        existing = authfile.read_root(base)
        doc = authfile.build_document(
            {"uid": cred.uid, "account": cred.account, "auth": cred.auth}, existing
        )
        try:
            authfile.write_document(doc, path=authfile.auth_path(base), expect_uid=cred.uid)
        except authfile.AuthWriteError as exc:
            _rollback(report, base)
            report.error = f"{exc}。已回滚到原来的登录账号。"
            return report
        report.auth_written = True

        # 切过去之后，目标账号就是"当前账号"了，顺手更新一次采集
        try:
            store.capture_current(base)
        except OSError:
            pass
    finally:
        # ---- 8. 无论成败都尝试拉起客户端：用户不能因为我们出错而失去客户端
        if plan.restart:
            say("重新启动 WorkBuddy…")
            report.restarted = closeflow.restart(outcome.exe_path)
            if not report.restarted:
                report.warnings.append(
                    f"自动启动失败，请手动打开：{outcome.exe_path or '（未能定位 WorkBuddy.exe）'}"
                )

    report.ok = report.auth_written
    return report


def _rollback(report: SwitchReport, base) -> None:
    """把备份的认证文件写回原位。尽力而为，失败要明说。"""
    if report.auth_backup is None:
        report.warnings.append("没有可用的认证文件备份，登录态可能停在未知状态")
        return
    import shutil

    try:
        shutil.copy2(report.auth_backup, authfile.auth_path(base))
    except OSError as exc:
        report.warnings.append(f"回滚认证文件失败：{exc.strerror or exc}")
        return
    if authfile.current_uid(base) != report.current_uid:
        report.warnings.append("回滚后当前账号与切换前不一致，请检查 WorkBuddy 登录状态")


def selftest_line(base: str | Path | None = None) -> str:
    store = vault.Vault.load(base)
    if not store:
        return "switch: 无已存凭据（需先用各账号登录一次客户端）"
    current = authfile.current_uid(base)
    others = [u for u in store.uids() if u != current]
    return f"switch: 接口就绪, 可切换目标 {len(others)} 个, 当前 {current[:8] or '未知'}"
