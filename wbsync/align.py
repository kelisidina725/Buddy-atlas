"""本机账号对齐 —— 让这台机器上的每个账号都拥有同一份会话与人格文件。

## 为什么是「并集」而不是「搬移」

用户的原话是「把本机的全部账号会话和配置弄成一样的」，也就是**切到哪个账号
看到的都一样**。搬移（A 的会话转到 B，A 就空了）做不到这件事，只有并集可以。

并集的技术支点是 v1.5 的会话复制：`sessions.id` 是主键，所以「一条会话属于
两个账号」不存在；但正文按 cid 命名，**造一个新 cid 就能并出第二份**。
于是：

    每条会话在每个账号下各有一份，cid 互不相同，正文是实体副本

## 代价必须写在计划页上

正文是**实体复制**，不是共享引用（早期设计稿说「按 cid 共享、磁盘增量 MB 级」
是错的，已订正）。3 个账号 ≈ 会话库总量 × (账号数 - 1) 的额外占用。
本机 60 MB 会话 → 对齐后约 180 MB。用户看不到这个数就不会理解为什么要等。

## 「已复制过」的判据不能是「目标账号里有同名会话」

复制过去的是**新 cid**，所以「目标里有没有这个 cid」永远是真（那是源的那份，
在源账号名下）。按 cid 判重会每次对齐都重复制一遍，两轮下来每个账号里堆着
一堆内容相同的副本。

判据用 `copysession` 自有的账本 `wbsync-copies.json`：`(源 cid, 目标 uid)`
→ 副本 cid。账本里有、且副本还活着 → 跳过；副本被删了 → 允许重新复制。

## 但账本只认「本工具复制过的」，还有第二级判重

账本空着不代表没有副本：客户端自身、参考工具、换机搬运都可能造出**同内容不同 cid**
的副本。实测本机账本 0 条，却有三组账号名下各存着同一批对话（16 组同标题、44 条），
其中 14 组在 cid 归一后完全一致。只按血统判重 ⇒ 这些全被当成「目标独有」再复制一遍，
对齐后同一对话在同一账号的侧边栏里出现两次。

所以加了内容判重：`fingerprint.fingerprint_of_file` 会把本会话 cid 换成占位符再取
全文摘要，摘要相同即认定「目标其实已有这份内容」。两层判据是**或**的关系。

★ 先按字节数预筛：副本只是 cid 等长替换，正文尺寸必然一致；尺寸不同则内容必然不同。
这条把「读 220 MB」压到「读几十 MB」，且不损失正确性。

## 还有第四级：这一次清单内部也要判重

前三级问的都是「目标账号里**已经**有没有这条」。但并集对齐的输入是多个源账号，
同一条对话在 A、B 名下各存一份是常态（换机搬运、参考工具、客户端自身都会造副本）：
cid 不同、字节数与正文完全一致。逐源判重时两边都答「目标没有」⇒ 两份一起排进
清单 ⇒ 目标账号里同一段对话出现两次，用户的原话是「复制的会话都有重复的一份」。

`_dedup_in_plan()` 在目标侧做合并：先按血统（账本有记录时零成本），再按字节数
预筛后的归一化摘要。命中的标 `same_content_of`（显示、可勾、默认不勾），
不是删掉 —— 与第三级同一口径。

本机实测：账本 0 条（血统判重全失效）、4 个账号、37 组兄弟副本，
4 个目标上合计多排了 39 条待复制项（197 → 158 条）。

## 人格文件只补不覆盖

目标账号里已有同名文件但内容不同，记为**冲突**并跳过。自动覆盖等于替用户
决定「哪一份人格是对的」—— 而他可能特意给测试号写了另一份 USER.md。
冲突在报告里逐条列出，由人决定。

## 顺序

    备份 → 关客户端 → 复制会话 → 补人格 → 合并记忆 → 重启

备份放在关客户端之后（客户端运行时复制 db 会读到不一致的页），
但**必须在任何写入之前**。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    accounts,
    closeflow,
    config_items,
    copysession,
    dbcopy,
    fingerprint,
    fpcache,
    guard,
    memorymerge,
    paths,
    uistate,
    winproc,
)

# ---------------------------------------------------------------- 数据结构

# 同族判定：目标账号里那条已有副本与源的关系
FAMILY_OLDER = "older"      # 目标落后，可追加
FAMILY_NEWER = "newer"      # 目标更新，源那份是旧的
FAMILY_DIVERGE = "diverge"  # 两边在公共部分之后各自长过，不能自动合并

# 计划内内容判重的最小正文尺寸（字节）。低于它只信血统，不比内容：
# 极短的正文（空会话、只有一行 session-meta）把自身 cid 抹掉后几乎必然相同，
# 拿它当「同一份对话的两份副本」会把两条互不相干的短会话合成一条。
# 本机 4 个账号 197 条会话里最小的一条是 101 KB，这个门槛只挡异常短的那批。
MIN_DEDUP_BYTES = 256


@dataclass
class SyncItem:
    """目标账号里已有同一份会话的**旧版本**：追加缺的行，而不是再复制一份。

    没有这一层时，同一段对话在 A 聊到 2307 行、在 B 停在 2132 行，会被当成
    两条独立会话各复制一份 —— 每个账号里同一标题躺着两份，用户无从分辨哪份是新的。
    """

    cid: str
    target_cid: str
    title: str = ""
    src_lines: int = 0
    dst_lines: int = 0
    common: int = 0
    body: Path | None = None
    target_body: Path | None = None
    updated_at: int = 0
    status: str = FAMILY_OLDER
    cwd: str = ""
    # 用户在「逐条选择」里的勾选。只有 older 可勾；分叉/目标更新永远不执行。
    selected: bool = True

    @property
    def add_lines(self) -> int:
        return max(0, self.src_lines - self.dst_lines)

    @property
    def actionable(self) -> bool:
        """只有「目标落后」且用户勾了才写。分叉要人判断，目标更新则无事可做。"""
        return self.status == FAMILY_OLDER and self.selected

    @property
    def conflict(self) -> str:
        if self.status == FAMILY_DIVERGE:
            return f"与目标里那份（{self.dst_lines} 行）已分叉，不会自动写入"
        return ""

    @property
    def kind_text(self) -> str:
        if self.status == FAMILY_DIVERGE:
            return "内容已分叉"
        if self.status == FAMILY_NEWER:
            return "目标里更新"
        return f"可追加 {self.add_lines} 行"


def _title_key(title: str) -> str:
    return (title or "").strip().lower()


class _FamilyIndex:
    """目标账号「同标题会话」索引 —— 用来认出一份会话的旧版本。

    ★ 为什么靠「同标题 + 全量公共前缀」而不是账本：账本只记录本工具复制过的
      副本。用户用客户端自身、参考工具、换机搬运产生的副本，账本里一条都没有
      （本机实测 16 组同标题会话，账本 0 条）。只认账本等于看不见它们。

    ★ 为什么必须有最小行数与比例门槛：jsonl 第一行是 `session-meta`，归一化后
      **所有会话的第一行都相同**，公共前缀至少是 1。只凭「有公共前缀」判同族，
      两条不相干的会话也会被凑成一对。
    """

    MIN_LINES = 8      # 短的那份至少这么多行才值得比
    MIN_RATIO = 0.9    # 公共前缀至少要覆盖短的那份的九成

    def __init__(self, uids: list[str], base=None) -> None:
        self._bodies = copysession._bodies_index(base)
        self._by_uid_title: dict[str, dict[str, list[str]]] = {}
        self._hashes: dict[str, list[int] | None] = {}
        wanted = {u for u in uids if u}
        db = paths.workbuddy_db_path(base)
        if not db.is_file():
            return
        try:
            with dbcopy.readonly_db_copy(db) as conn:
                rows = conn.execute(
                    "SELECT id, user_id, "
                    "COALESCE(NULLIF(custom_title, ''), title, '') "
                    'FROM "sessions" WHERE deleted_at IS NULL'
                ).fetchall()
        except sqlite3.Error:
            return
        for cid, uid, title in rows:
            uid = str(uid or "")
            key = _title_key(title)
            if uid not in wanted or not key:
                continue
            self._by_uid_title.setdefault(uid, {}).setdefault(key, []).append(str(cid))

    def body_of(self, cid: str) -> Path | None:
        found = self._bodies.get(cid, [])
        return found[0] if found else None

    def _line_hashes(self, cid: str) -> list[int] | None:
        if cid in self._hashes:
            return self._hashes[cid]
        got: list[int] | None = None
        for p in self._bodies.get(cid, []):
            got = fingerprint.full_line_hashes_of_file(p, cid)
            if got is not None:
                break
        self._hashes[cid] = got
        return got

    def match(self, uid: str, item: copysession.CopyItem) -> SyncItem | None:
        """目标账号里是否有与该 item 同族的副本。None = 不是同族（照常复制）。"""
        key = _title_key(item.title)
        if not key:
            return None
        cands = self._by_uid_title.get(uid, {}).get(key)
        if not cands:
            return None
        src_h = self._line_hashes(item.cid)
        if not src_h:
            return None

        best: SyncItem | None = None
        for tc in cands:
            if tc == item.cid:
                continue
            tgt_h = self._line_hashes(tc)
            if not tgt_h:
                continue
            short = min(len(src_h), len(tgt_h))
            if short < self.MIN_LINES:
                continue
            common = fingerprint.common_prefix_of(src_h, tgt_h)
            if common < max(self.MIN_LINES, int(short * self.MIN_RATIO)):
                # 公共部分太少 ⇒ 判为两条不相干的会话，不去动它
                continue
            if common == short:
                status = FAMILY_OLDER if len(src_h) > len(tgt_h) else FAMILY_NEWER
            else:
                status = FAMILY_DIVERGE
            cand = SyncItem(
                cid=item.cid,
                target_cid=tc,
                title=item.title,
                src_lines=len(src_h),
                dst_lines=len(tgt_h),
                common=common,
                body=item.body,
                target_body=self.body_of(tc),
                updated_at=item.updated_at,
                status=status,
                cwd=item.cwd,
            )
            # 同一标题命中多条时，优先「可追加且增量最小」的那条
            if best is None:
                best = cand
            elif cand.actionable and not best.actionable:
                best = cand
            elif cand.actionable and best.actionable and cand.add_lines < best.add_lines:
                best = cand
        return best


@dataclass
class PersonaAction:
    """一个目标账号的人格文件处理结果（计划阶段只是意图）。"""

    add: list[str] = field(default_factory=list)        # 目标缺失、将补齐
    conflict: list[str] = field(default_factory=list)   # 已有且不同、不覆盖
    same: list[str] = field(default_factory=list)       # 已有且相同、无需动作

    @property
    def changed(self) -> bool:
        return bool(self.add)


@dataclass
class AlignTarget:
    """一个目标账号要对齐成什么样。"""

    uid: str
    name: str
    copies: list[copysession.CopyItem] = field(default_factory=list)
    # 全部候选（含重复、含不可复制的）。`copies` 只是其中默认勾选的那部分，
    # 供「逐条选择」界面展示 —— 用户要看得见「哪些被跳过了、为什么」。
    items: list[copysession.CopyItem] = field(default_factory=list)
    # 目标里已有同一份会话的旧版本：追加增量，而不是再复制一份。
    syncs: list[SyncItem] = field(default_factory=list)
    persona: PersonaAction = field(default_factory=PersonaAction)
    memory: bool = False
    blocked: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def dup_content(self) -> int:
        """清单里被标注为重复、且**当前没被勾选**的条数。

        由数据实时算出而不是记账：用户在「选择会话」里把某条重复项勾上之后，
        「已跳过 N 条」必须立刻跟着变，否则数字与列表自相矛盾。
        """
        chosen = {i.cid for i in self.copies}
        return sum(1 for i in self.items if i.duplicate and i.cid not in chosen)

    @property
    def body_bytes(self) -> int:
        return sum(i.body_bytes for i in self.copies)

    @property
    def sync_actions(self) -> list[SyncItem]:
        """真正会写盘的追加动作（分叉与「目标里更新」都不算）。"""
        return [s for s in self.syncs if s.actionable]

    @property
    def sync_lines(self) -> int:
        return sum(s.add_lines for s in self.sync_actions)

    @property
    def empty(self) -> bool:
        return (
            not self.copies
            and not self.sync_actions
            and not self.persona.add
            and not self.memory
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.copies:
            parts.append(f"新增 {len(self.copies)} 条会话")
        if self.sync_actions:
            parts.append(f"追加 {len(self.sync_actions)} 条会话的增量")
        if self.persona.add:
            parts.append(f"补 {len(self.persona.add)} 个人格文件")
        if self.memory:
            parts.append("合并长期记忆")
        return " · ".join(parts) or "已是最新"

    def detail_lines(self) -> list[str]:
        """计划页每个账号下面那几行 —— **只留真正会写盘的动作**。

        用户的原话是「只提示复制多少条会话，约 xxMB」。所以这里最多两行：

            复制 44 条会话（约 104.8 MB）
            追加 1 条会话的增量（共 375 行） · 合并长期记忆

        「跳过 N 条」这类只读信息一律不进列表（它会跟主数字抢注意力），
        原因、条数与标题清单都折进 `detail_hint()` 的悬停提示。
        """
        out: list[str] = []
        if self.copies:
            out.append(f"复制 {len(self.copies)} 条会话（约 {_human(self.body_bytes)}）")
        tail: list[str] = []
        if self.persona.add:
            tail.append(f"补 {len(self.persona.add)} 个人格文件")
        if self.memory:
            tail.append("合并长期记忆")
        if self.sync_actions:
            # 追加比复制危险（它改写目标账号里已有的正文），值得单列，
            # 但不必为它多占一行 —— 人格/记忆本来就是顺手带上的
            line = f"追加 {len(self.sync_actions)} 条会话的增量（共 {self.sync_lines} 行）"
            out.append(line + (" · " + " · ".join(tail) if tail else ""))
        elif tail:
            out.append("另：" + " · ".join(tail))
        if not out:
            out.append("已是最新，无需改动")
        return out

    def skipped_line(self) -> str:
        """被跳过的东西压成一行（只进悬停提示，不进主列表）。"""
        bits: list[str] = []
        if self.dup_content:
            bits.append(f"{self.dup_content} 条已有相同内容")
        if self.blocked:
            bits.append(f"{self.blocked} 条不可复制")
        diverge = sum(1 for s in self.syncs if s.status == FAMILY_DIVERGE)
        if diverge:
            bits.append(f"{diverge} 条已分叉")
        newer = sum(1 for s in self.syncs if s.status == FAMILY_NEWER)
        if newer:
            bits.append(f"{newer} 条在目标里更新")
        if self.persona.conflict:
            bits.append(f"{len(self.persona.conflict)} 个人格文件不覆盖")
        return ("跳过 " + " · ".join(bits)) if bits else ""

    def detail_hint(self) -> str:
        """悬停提示：主列表里没写的那些数字、原因与标题清单。"""
        out: list[str] = []
        skipped = self.skipped_line()
        if skipped:
            out.append(skipped)
        if self.sync_actions:
            names = "、".join(
                f"{s.title or s.cid[:8]} +{s.add_lines} 行" for s in self.sync_actions[:5]
            )
            out.append(f"可追加：{names}")
        diverge = [s for s in self.syncs if s.status == FAMILY_DIVERGE]
        if diverge:
            out.append(
                "已分叉（两侧在共同旧版之后各自长过，自动合并会丢消息，只报告不写入）："
                + "、".join(s.title or s.cid[:8] for s in diverge[:5])
            )
        newer = [s for s in self.syncs if s.status == FAMILY_NEWER]
        if newer:
            out.append(
                "目标里已是更新的版本（复制过去等于回退，故跳过）："
                + "、".join(s.title or s.cid[:5] for s in newer[:5])
            )
        if self.persona.add:
            out.append("补齐人格文件：" + "、".join(self.persona.add))
        if self.persona.conflict:
            out.append(
                "目标账号已有且内容不同的人格文件，**不覆盖**："
                + "、".join(self.persona.conflict)
            )
        if self.dup_content:
            out.append(
                "「已有相同内容」= 该账号前台已有这段对话，或本次清单里已有一条"
                "内容相同的（只是会话 id 不同）；两条都复制过去会让它在侧边栏里出现两次"
            )
        if self.blocked:
            out.append(f"{self.blocked} 条不可复制（云端会话 / Claw 工作区 / 缺正文）")
        return "\n".join(out)


@dataclass
class AlignPlan:
    """对齐计划。`plan_align` 只读。"""

    primary_uid: str = ""
    primary_name: str = ""
    uids: list[str] = field(default_factory=list)
    targets: list[AlignTarget] = field(default_factory=list)
    db_path: Path | None = None
    edge_db_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_copies(self) -> int:
        return sum(len(t.copies) for t in self.targets)

    @property
    def total_bytes(self) -> int:
        return sum(t.body_bytes for t in self.targets)

    @property
    def actionable(self) -> list[AlignTarget]:
        return [t for t in self.targets if not t.empty]

    @property
    def ready(self) -> bool:
        """一条都不需要做的时候不该假装"可以执行"。"""
        return bool(self.actionable) and bool(self.primary_uid)

    def headline(self) -> str:
        if not self.primary_uid:
            return "未能识别本机登录账号，无法对齐"
        if not self.targets:
            return "没有勾选要接收内容的账号"
        acts = self.actionable
        if not acts:
            return "勾选的账号已经是同一份数据，无需对齐"
        parts: list[str] = []
        if self.total_copies:
            parts.append(f"{self.total_copies} 条会话")
        syncs = sum(len(t.sync_actions) for t in self.targets)
        if syncs:
            parts.append(f"{syncs} 条会话的增量追加")
        persona = sum(len(t.persona.add) for t in self.targets)
        if persona:
            parts.append(f"{persona} 个人格文件")
        memory = sum(1 for t in self.targets if t.memory)
        if memory:
            parts.append(f"{memory} 个账号的长期记忆")
        # 不带"将为"这种含糊说法：只写真实会发生的事
        tail = f"（正文新增约 {_human(self.total_bytes)}）" if self.total_bytes else ""
        return f"把 {len(acts)} 个账号对齐：将新增 " + "、".join(parts) + tail

    def steps(self) -> list[str]:
        out = ["备份会话库与正文清单", "关闭 WorkBuddy"]
        if self.total_copies:
            out.append(f"为 {len(self.actionable)} 个账号复制 {self.total_copies} 条会话")
        syncs = sum(len(t.sync_actions) for t in self.targets)
        if syncs:
            out.append(f"把 {syncs} 条会话的增量追加到目标账号的旧版本里")
        persona = sum(len(t.persona.add) for t in self.targets)
        if persona:
            out.append(f"补齐 {persona} 个人格文件")
        memory = sum(1 for t in self.targets if t.memory)
        if memory:
            out.append(f"合并 {memory} 个账号的长期记忆")
        out.append("重新启动 WorkBuddy")
        return out


@dataclass
class AlignReport:
    """对齐结果。"""

    copied: dict[str, int] = field(default_factory=dict)      # uid -> 复制条数
    appended: dict[str, int] = field(default_factory=dict)    # uid -> 追加条数
    appended_lines: int = 0
    append_failed: list[tuple[str, str]] = field(default_factory=list)
    persona_added: int = 0
    persona_conflicts: list[tuple[str, str]] = field(default_factory=list)
    memory_merged: list[str] = field(default_factory=list)
    backup: guard.Backup | None = None
    body_bytes: int = 0
    restarted: bool | None = None
    edge_registered: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    ok: bool = False

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.copied:
            total = sum(self.copied.values())
            out.append(f"已复制 {total} 条会话到 {len(self.copied)} 个账号（正文 {_human(self.body_bytes)}）")
        if self.appended:
            total = sum(self.appended.values())
            out.append(f"已把 {total} 条会话的增量追加到目标账号的旧版本（共 {self.appended_lines} 行）")
        if self.append_failed:
            out.append("以下会话追加失败（内容在计划后被改动过？原文件未修改）：")
            out += [f"  · {name}：{title}" for name, title in self.append_failed]
        if self.persona_added:
            out.append(f"补齐 {self.persona_added} 个人格文件")
        if self.memory_merged:
            out.append(f"合并了 {len(self.memory_merged)} 个账号的长期记忆")
        if self.persona_conflicts:
            out.append("以下人格文件目标账号已有且内容不同，未覆盖：")
            out += [f"  · {name}：{fname}" for name, fname in self.persona_conflicts]
        if self.backup is not None:
            out.append(f"备份：{self.backup.root.name}")
        if self.error:
            out.append(self.error)
        out += [f"注意：{w}" for w in self.warnings]
        return out


def _dedup_sort(items: list[copysession.CopyItem]) -> list[copysession.CopyItem]:
    """同一 cid 只留一份，再按最近更新排序（界面按这个顺序显示）。"""
    seen: set[str] = set()
    out: list[copysession.CopyItem] = []
    for item in items:
        if item.cid in seen:
            continue
        seen.add(item.cid)
        out.append(item)
    out.sort(key=lambda i: i.updated_at, reverse=True)
    return out


def _dedup_syncs(items: list[SyncItem]) -> list[SyncItem]:
    """同一个目标旧版本只保留**一条**追加动作。

    ★ 为什么必须去重：两条不同的源会话可能是同一个目标副本的「兄弟副本」
      （历史搬运留下的）。各自匹配成功一次，执行时就会连续追加两遍 ——
      2132 → 2307 → 2482，同一段内容被写进去两次。
      保留增量最大的那条（它最新）。
    """
    best: dict[str, SyncItem] = {}
    order: list[str] = []
    for s in items:
        cur = best.get(s.target_cid)
        if cur is None:
            best[s.target_cid] = s
            order.append(s.target_cid)
        elif s.add_lines > cur.add_lines:
            best[s.target_cid] = s
    return [best[k] for k in order]


def _dedup_in_plan(target: "AlignTarget", digest_of, produced: dict[str, str]) -> int:
    """同一个目标在**一次计划内**不该收到两份内容等价的会话。

    ## 这一级为什么必须存在

    前面三级判重都在回答「目标账号里**已经**有没有这条」。但并集对齐的输入是
    多个源账号，同一条对话在 A、B 名下各存一份是常态（换机搬运、参考工具、
    客户端自身都会造副本）—— 两条 cid 不同，字节数与正文完全一致。逐源判重时
    两边都答「目标没有」，于是两份一起排进清单，目标账号里同一段对话出现两次。
    用户看到的就是「复制的会话都有重复的一份」。

    本机实测：账本 0 条（血统判重全失效）、4 个账号、37 组这样的兄弟副本，
    4 个目标上合计多排了 39 条待复制项（197 → 158 条）。

    ## 判据与代价

    先按血统（`canonical_cid`）：账本有记录时零成本。
    再按内容：**只对字节数在本目标撞车的项**读正文取归一化摘要 —— 副本是 cid
    等长替换，尺寸必然相同；尺寸在本目标里唯一，就不可能重复，不值得读盘。

    命中的那条不删除，只标 `same_content_of`：清单要显示、要能勾（用户可能
    执意要两份），但默认不勾 —— 与 `_ContentDedup` 的口径一致。
    """
    cands = [i for i in target.items if not i.block_reason and not i.duplicate]
    sizes: dict[int, int] = {}
    for i in cands:
        if i.body_bytes >= MIN_DEDUP_BYTES:
            sizes[i.body_bytes] = sizes.get(i.body_bytes, 0) + 1

    kept_by_size: dict[int, dict[str, str]] = {}
    seen_canon: dict[str, str] = {}
    dropped = 0
    # 保留更新的那条：内容一致时 updated_at 大者更可能是用户最近在用的那份，
    # 且排序固定（再按 cid），计划两次跑出来的结果不会漂移。
    for item in sorted(cands, key=lambda i: (-i.updated_at, i.cid)):
        canon = copysession.canonical_cid(item.cid, produced)
        twin = seen_canon.get(canon)
        if twin:
            item.same_content_of = twin
            dropped += 1
            continue
        if sizes.get(item.body_bytes, 0) >= 2:
            digest = digest_of(item.cid) if digest_of else ""
            if digest:
                bucket = kept_by_size.setdefault(item.body_bytes, {})
                twin = bucket.get(digest)
                if twin:
                    item.same_content_of = twin
                    dropped += 1
                    continue
                bucket[digest] = item.cid
        seen_canon[canon] = item.cid

    if dropped:
        target.copies = [i for i in target.copies if not i.same_content_of]
    return dropped


def _human(n: int) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1048576:
        return f"{n / 1024:.0f} KB"
    if n < 1073741824:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1073741824:.2f} GB"


# ---------------------------------------------------------------- 路径


def persona_dir_of(uid: str, base=None) -> Path:
    """账号的人格目录。

    `config_items.persona_dir()` 只返回排序第一个，多账号机器上会拿错人的目录；
    这里按 uid 精确定位，并兼容早期只有 `<uid>/` 的状态。
    """
    root = paths.session_data_dir(base)
    for name in (f"user-{uid}-personal", uid):
        p = root / name
        if p.is_dir():
            return p
    return root / f"user-{uid}-personal"


# ---------------------------------------------------------------- 计划


class _ContentDedup:
    """第二级判重：目标账号里是否已有「内容等价、只是 cid 不同」的副本。

    只在**字节数撞上同账号某条会话**时才读正文取摘要：
    副本是 cid 等长替换，尺寸必然相同；尺寸不同则内容必然不同，不值得读盘。

    摘要复用 `fingerprint` 的归一化全文 sha256（它会把本会话 cid 换成占位符），
    所以「同一批对话的两个副本」判等成立，而「同标题但内容不同」不会被误合并。
    """

    def __init__(self, uids: list[str], base=None) -> None:
        self.base = base
        self.evaluated = 0                      # 因字节数撞上而真的读了正文的条数
        self._bodies = copysession._bodies_index(base)
        self._groups: dict[str, dict[int, list[str]]] = {}
        self._digests: dict[tuple[str, int], dict[str, str]] = {}
        self._cache = fpcache.open_cache(base)

        wanted = {u for u in uids if u}
        db = paths.workbuddy_db_path(base)
        if not db.is_file():
            return
        try:
            with dbcopy.readonly_db_copy(db) as conn:
                rows = conn.execute(
                    'SELECT id, user_id FROM "sessions" WHERE deleted_at IS NULL'
                ).fetchall()
        except sqlite3.Error:
            # 读不到就不做内容判重 —— 退回只按血统判重，宁可多复制也不误跳过
            return
        # ★ 只认「未删除」：目标那条已删除的话，用户切过去看不到，
        #   跳过就等于对齐没做到。
        for cid, uid in rows:
            uid = str(uid or "")
            if uid not in wanted:
                continue
            found = self._bodies.get(str(cid))
            if not found:
                continue
            # LF 口径（见 fingerprint.lf_size）：摘要对换行符不敏感，预筛也必须如此，
            # 否则 LF/CRLF 两种写法的同一段对话永远撞不上车
            size = fingerprint.lf_size(found[0])
            if size is None:
                continue
            self._groups.setdefault(uid, {}).setdefault(size, []).append(str(cid))

    def close(self) -> None:
        self._cache.close()

    def _digest_of(self, cid: str) -> str:
        for p in self._bodies.get(cid, []):
            fp = self._cache.resolve(cid, p)
            if fp is not None and fp.digest:
                return fp.digest
        return ""

    def _target_digests(self, uid: str, size: int) -> dict[str, str]:
        """该账号在这一档字节数下拥有的内容：摘要 → 第一条 cid。"""
        key = (uid, size)
        got = self._digests.get(key)
        if got is None:
            got = {}
            for c in self._groups.get(uid, {}).get(size, []):
                d = self._digest_of(c)
                if d and d not in got:
                    got[d] = c
            self._digests[key] = got
        return got

    def digest_of(self, cid: str) -> str:
        """任一会话的归一化全文摘要（把自身 cid 换成占位符后取 sha256）。

        暴露出来是给「本次计划内判重」用的：那一步要比较两条**都还没进目标**
        的会话，不经过 `same_content` 的 uid 分桶。
        """
        return self._digest_of(cid)

    def same_content(self, uid: str, item: copysession.CopyItem) -> str:
        """目标账号是否已拥有与该 item 内容等价的会话。

        返回命中那条会话的 cid（供界面标注「与哪条重复」），没有则返回空串。
        """
        sizes = self._groups.get(uid)
        if not sizes or item.body_bytes <= 0 or item.body_bytes not in sizes:
            return ""
        mine = self._digest_of(item.cid)
        if not mine:
            return ""
        self.evaluated += 1
        return self._target_digests(uid, item.body_bytes).get(mine, "")


def _backup_body_file(root: Path, path: Path) -> None:
    """把即将被追加改写的正文复制进本次备份目录的 `bodies/` 下。

    只备份**被改的那几条**，不是整库快照 —— 回滚一条会话不需要 200 MB。
    """
    dst = root / "bodies" / path.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)


def plan_align(
    uids: list[str],
    labels: accounts.AccountLabels | None = None,
    base=None,
    do_sessions: bool = True,
    do_persona: bool = True,
    do_memory: bool = True,
    primary_uid: str = "",
    target_uids: list[str] | None = None,
) -> AlignPlan:
    """算出对齐计划。只读，不改任何文件。

    `uids`          —— 作为**来源**参与的账号：它们的会话可以流进目标。
    `target_uids`   —— 只给这些账号生成目标（`None` = `uids` 全部）。
                       传空列表 ⇒ 一个目标都不收，计划因此不可执行。

    ★ 来源与目标分开，是因为「只想补某个新号」是常见诉求：只勾一个账号时，
      其余账号原样不动，那个账号拿到所有人的会话。若把来源也限制成勾选集合，
      「只补新号」就变成了「新号与谁对齐」——用户还得再把别人勾上，多此一举。
    """
    labels = labels or accounts.AccountLabels.load(base)
    plan = AlignPlan(db_path=paths.workbuddy_db_path(base))
    plan.edge_db_path = paths.edge_sync_db_path(base)

    clean = [u for u in dict.fromkeys(uids) if u]
    # None = 不限制（全部账号都收）；空集合 = 一个都不收
    only = None if target_uids is None else {u for u in target_uids if u}
    plan.uids = clean
    plan.primary_uid = primary_uid or labels.primary_uid or accounts.detect_primary_uid(base)
    plan.primary_name = labels.name_of(plan.primary_uid) if plan.primary_uid else ""

    if not plan.primary_uid:
        plan.warnings.append("未识别到本机登录账号：无法确定人格文件的基准来源")
    if len(clean) < 2:
        plan.warnings.append("本机只检测到 1 个账号，没有可对齐的对象")
    if only is not None and not only:
        plan.warnings.append("没有勾选要接收内容的账号：本次不会复制任何会话")

    source_persona = (
        persona_dir_of(plan.primary_uid, base) if plan.primary_uid else None
    )
    source_files = _persona_files(source_persona) if source_persona else {}

    ledger = copysession.load_ledger(base) if do_sessions else {}
    produced = copysession.lineage(ledger)

    # 每个账号「前台看得见的内容」——按血统归一后的 cid 集合。
    # 判「目标是否已经有这条」用它，而不是用原始 cid：副本 cid 与原件不同，
    # 按原始 cid 判会让并集每次都重跑一遍并把副本数量翻倍。
    #
    # ★ 只算「未删除」：目标账号里那条如果已被删除，用户切过去**看不到**，
    #   算作「已有」等于对齐没做到。本机实测 4 个账号里有 2 个存在这种记录
    #   （血统集合 63 条里 25 条已删、57 条里 37 条已删）——
    #   按含删除的口径判重，这些人切过去会少掉一批会话。
    owned: dict[str, set[str]] = {}
    if do_sessions:
        for uid in clean:
            owned[uid] = {
                copysession.canonical_cid(c, produced)
                for c in copysession.alive_session_cids(uid, base)
            }

    # 第二级判重（内容等价、只是 cid 不同）。账本为空时这一层才是主要防线。
    dedup = _ContentDedup(clean, base) if do_sessions else None
    # 第三级：目标里是同一份会话的**旧版本** ⇒ 追加增量，而不是再复制一份
    family = _FamilyIndex(clean, base) if do_sessions else None

    for uid in clean:
        if only is not None and uid not in only:
            continue          # 未被勾选为接收方：一个字节都不写
        target = AlignTarget(uid=uid, name=labels.name_of(uid))
        # ★ 登录账号也要当目标：并集是双向的，只把别人的会话搬进来而不把自己的
        #   搬出去，结果是「切过去看得到全部、切回来只有自己那一份」，正好反了。
        #   人格文件那里它会自然全是「相同」，不需要单独跳过。

        # ---- 会话：从其他每个账号各算一份，合并到本目标
        if do_sessions:
            for src in clean:
                if src == uid:
                    continue
                sub = copysession.plan_copy(target_uid=uid, source_uid=src, base=base)
                for item in sub.items:
                    # 账本记过的重复项：保留在清单里（标注「已复制过」），默认不勾选。
                    # 直接丢掉会让用户以为「源账号只有这么多会话」，反而更困惑。
                    if item.duplicate_of:
                        target.items.append(item)
                        continue
                    if item.block_reason:
                        target.blocked += 1
                        target.items.append(item)
                        continue
                    # 血统判重：目标拥有的是**同一条会话**（原件或它的副本），
                    # 连展示都不必 —— 列出来只会是同一个对话的两个 id。
                    if copysession.canonical_cid(item.cid, produced) in owned.get(uid, set()):
                        continue
                    hit = dedup.same_content(uid, item) if dedup is not None else ""
                    if hit:
                        # 内容等价但 cid 不同：展示、标注、默认不勾
                        item.same_content_of = hit
                        target.items.append(item)
                        continue
                    # 目标里是同一份会话的**旧版本** ⇒ 追加缺的行，不再造第二份。
                    # 这一步必须排在「加入 copies」之前，否则会先被复制一遍，
                    # 目标账号里就同时存在新旧两份同名会话了。
                    fam = family.match(uid, item) if family is not None else None
                    if fam is not None:
                        target.syncs.append(fam)
                        continue
                    target.copies.append(item)
                    target.items.append(item)
                for w in sub.warnings:
                    if w not in target.warnings:
                        target.warnings.append(w)
            # 同一 cid 可能被多个源列到（不会，主键保证），但仍去重兜底
            target.copies = _dedup_sort(target.copies)
            target.items = _dedup_sort(target.items)
            target.syncs = _dedup_syncs(target.syncs)
            # ★ 第四级：**本次计划内**判重。前面三级都只问「目标账号里有没有」，
            #   而两个源账号可能各存着同一条对话的一份（cid 不同、内容相同）。
            #   不在这里合并，目标就会被复制出两份同名同内容的会话。
            if dedup is not None:
                _dedup_in_plan(target, dedup.digest_of, produced)

        # ---- 人格文件：以登录账号为基准，只补缺失
        if do_persona and source_files:
            dst_dir = persona_dir_of(uid, base)
            for fname, data in sorted(source_files.items()):
                existing = dst_dir / fname
                if not existing.is_file():
                    target.persona.add.append(fname)
                    continue
                try:
                    if existing.read_bytes() == data:
                        target.persona.same.append(fname)
                    else:
                        target.persona.conflict.append(fname)
                except OSError:
                    target.persona.conflict.append(fname)

        # ---- 记忆：有内容才做。登录账号自己不合并（源就是它，自合并是空操作）
        if do_memory and plan.primary_uid and uid != plan.primary_uid:
            src_mem = memorymerge.memory_path(plan.primary_uid, base)
            if src_mem.is_file() and src_mem.stat().st_size > 0:
                target.memory = True

        plan.targets.append(target)

    # 落盘指纹缓存（只影响下次加速，写失败无害）
    if dedup is not None:
        dedup.close()

    # 计划页最该看见的一句话：磁盘会变多少
    if plan.total_bytes:
        plan.warnings.append(
            f"会话正文是实体复制，本次会多占约 {_human(plan.total_bytes)} 磁盘。"
            "复制出的副本与原件各自独立：删掉任何一份都不影响另一份。"
        )
    return plan


def _persona_files(d: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name, _label in config_items.PERSONA_FILES:
        p = d / name
        if p.is_file():
            try:
                out[name] = p.read_bytes()
            except OSError:
                continue
    return out


# ---------------------------------------------------------------- 执行


def execute_align(
    plan: AlignPlan,
    base=None,
    progress=None,
    restart: bool = True,
) -> AlignReport:
    """执行对齐。会先关闭客户端；任一致命失败都停下并说明。"""
    report = AlignReport(warnings=list(plan.warnings))
    if not plan.ready:
        report.error = plan.headline()
        return report

    def say(text: str) -> None:
        if progress is not None:
            progress(text)

    # ---- 1. 关闭客户端（必须在备份之前：运行中的库复制出来是不一致的）
    say("关闭 WorkBuddy…")
    outcome = closeflow.prepare_import(
        progress=(lambda used, left: say(f"等待 WorkBuddy 退出… {used:.0f}s"))
        if progress
        else None
    )
    if not outcome.ok:
        report.error = f"关闭 WorkBuddy 失败：{outcome.message}。已中止，数据未改动。"
        return report

    try:
        # ---- 2. 备份（一次，覆盖库 + 云端映射）
        say("备份会话库…")
        db_files = [p for p in (plan.db_path, plan.edge_db_path) if p and p.is_file()]
        if db_files:
            try:
                report.backup = guard.create_backup(db_files, base=base, label="align")
            except OSError as exc:
                report.error = f"备份失败，已中止：{exc.strerror or exc}"
                return report

        # ---- 3. 逐目标复制会话
        first = True
        for target in plan.targets:
            if not target.copies:
                continue
            say(f"复制会话到「{target.name}」…")
            # 用户在「逐条选择」里勾上的重复项，`copyable` 为 False（duplicate_of
            # 非空），会被 execute_copy 当成"已复制过"静默丢掉。勾了就是要复制，
            # 这里显式清掉标记 —— 否则界面上勾了 3 条、实际只复制 2 条。
            chosen = [
                replace(i, duplicate_of="") if i.duplicate_of else i
                for i in target.copies
            ]
            sub = copysession.CopyPlan(
                source_uid=plan.primary_uid,
                target_uid=target.uid,
                items=chosen,
                db_path=plan.db_path,
                edge_db_path=plan.edge_db_path,
            )
            rep = copysession.execute_copy(
                sub,
                base=base,
                progress=(
                    lambda done, total, label, nm=target.name: say(f"{nm}：{label}")
                )
                if progress
                else None,
                # 第一次已由上面那次备份覆盖，后续不再重复备份整库
                backup=first,
            )
            first = False
            report.copied[target.uid] = len(rep.copied)
            report.body_bytes += rep.body_bytes
            report.edge_registered += rep.edge_registered
            report.warnings.extend(rep.warnings)
            for cid, msg in rep.failed:
                report.warnings.append(f"{target.name}：{cid} {msg}")

        # ---- 3.5 增量追加：**改写目标账号里已有的正文**，比复制危险
        #
        # 复制只新增文件，最坏是多几个没人引用的 jsonl。追加是就地改写用户
        # 正在用的会话，所以：先把被改的文件单独复制进本次备份目录，再写。
        # 备份缺失时宁可跳过（报告里说明），也不裸写。
        for target in plan.targets:
            acts = target.sync_actions
            if not acts:
                continue
            if report.backup is None:
                for s in acts:
                    report.append_failed.append((target.name, s.title or s.cid))
                report.warnings.append(
                    f"{target.name}：本次没有备份目录，{len(acts)} 条增量追加已跳过"
                )
                continue
            say(f"追加增量到「{target.name}」…")
            for s in acts:
                label = s.title or s.cid[:8]
                if s.body is None or s.target_body is None:
                    report.append_failed.append((target.name, label))
                    continue
                try:
                    _backup_body_file(report.backup.root, s.target_body)
                except OSError as exc:
                    report.warnings.append(
                        f"{target.name}：{label} 的正文备份失败"
                        f"（{exc.strerror or exc}），已跳过追加"
                    )
                    report.append_failed.append((target.name, label))
                    continue
                written = copysession.append_delta(
                    s.body, s.target_body, s.cid, s.target_cid, s.dst_lines
                )
                if written < 0:
                    report.append_failed.append((target.name, label))
                    continue
                report.appended[target.uid] = report.appended.get(target.uid, 0) + 1
                report.appended_lines += s.add_lines

        # ---- 4. 补人格文件
        added = 0
        for target in plan.targets:
            if not target.persona.add:
                continue
            src_dir = persona_dir_of(plan.primary_uid, base)
            dst_dir = persona_dir_of(target.uid, base)
            dst_dir.mkdir(parents=True, exist_ok=True)
            for fname in target.persona.add:
                src = src_dir / fname
                try:
                    data = src.read_bytes()
                except OSError:
                    continue
                try:
                    (dst_dir / fname).write_bytes(data)
                    added += 1
                except OSError as exc:
                    report.warnings.append(f"{target.name}：补 {fname} 失败（{exc.strerror or exc}）")
            for fname in target.persona.conflict:
                report.persona_conflicts.append((target.name, fname))
        report.persona_added = added

        # ---- 5. 合并长期记忆
        for target in plan.targets:
            if not target.memory:
                continue
            try:
                rep = memorymerge.merge(plan.primary_uid, target.uid, base=base)
                report.memory_merged.append(target.name)
                report.warnings.extend(rep.warnings)
            except (OSError, ValueError) as exc:
                report.warnings.append(f"{target.name}：记忆合并失败（已跳过）：{exc}")
    finally:
        # ---- 6. 无论成败都拉起客户端
        if restart:
            say("重新启动 WorkBuddy…")
            # 客户端是这一步之前就被关掉的，primary_exe 这时已经取不到路径；
            # 先兜回注册表/记忆里的安装位置，再退化成「报告失败」。
            exe = outcome.exe_path or winproc.resolve_client_exe(
                remembered=lambda: uistate.load_client_exe(base)
            )
            report.restarted = closeflow.restart(exe)
            if report.restarted and exe:
                uistate.save_client_exe(exe, base)
            if not report.restarted:
                report.warnings.append(
                    f"自动启动失败，请手动打开：{exe or '（未能定位 WorkBuddy.exe）'}"
                )

    report.ok = not report.error
    return report


def selftest_line(base=None) -> str:
    labels = accounts.AccountLabels.load(base)
    primary = labels.primary_uid or accounts.detect_primary_uid(base)
    ledger = copysession.load_ledger(base)
    return (
        f"align: 接口就绪, 登录账号 {primary[:8] or '未知'}, "
        f"已记复制 {len(ledger)} 条"
    )
