#!/usr/bin/env python3
"""Seed an agent's expert corpus with 4 Chinese-law sample sources.

Usage:
  python3 scripts/seed_legal_sample.py <agent_id>

Reads ~/.tudou_claw/expert/<agent_id>/corpus/_manifest.json, appends:
  - civil-code-chapter-1     民法典第一章总则 (10 articles)
  - nda-template             标准 NDA 保密协议
  - contract-review-checklist 合同审查 12 项
  - legal-fallacies          常见法律误区 Q&A

Each is chunked with the paragraph chunker and written as
chunks.jsonl under the source's directory. The agent must already be
cultivated as a legal expert (specialty='legal'); pre-existing
sources are NOT overwritten — same source_id will replace.

This is a development / demo helper. In production, corpus would be
ingested via the /api/portal/agent/{id}/expert/corpus/ingest API
(POST with `content` field).
"""
from __future__ import annotations

import json
import os
import sys
import time

# Make sure the repo root is importable when run from anywhere
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from app.domain_expert.corpus import chunker as ch
from app.domain_expert.corpus.manifest import CorpusManifest, CorpusSourceEntry
from app.domain_expert._config import expert_dir_for


# ─── Sample corpora (public-domain Chinese legal text excerpts) ─────────

CIVIL_CODE_CH1 = """第一章 总则

第一条 为了保护民事主体的合法权益,调整民事关系,维护社会和经济秩序,适应中国特色社会主义发展要求,弘扬社会主义核心价值观,根据宪法,制定本法。

第二条 民法调整平等主体的自然人、法人和非法人组织之间的人身关系和财产关系。

第三条 民事主体的人身权利、财产权利以及其他合法权益受法律保护,任何组织或者个人不得侵犯。

第四条 民事主体在民事活动中的法律地位一律平等。

第五条 民事主体从事民事活动,应当遵循自愿原则,按照自己的意思设立、变更、终止民事法律关系。

第六条 民事主体从事民事活动,应当遵循公平原则,合理确定各方的权利和义务。

第七条 民事主体从事民事活动,应当遵循诚信原则,秉持诚实,恪守承诺。

第八条 民事主体从事民事活动,不得违反法律,不得违背公序良俗。

第九条 民事主体从事民事活动,应当有利于节约资源、保护生态环境。

第十条 处理民事纠纷,应当依照法律;法律没有规定的,可以适用习惯,但是不得违背公序良俗。"""

NDA_TEMPLATE = """保密协议(NDA)模板

第一条(目的) 双方为开展业务合作,可能向对方披露保密信息,为保护双方权益,特订立本协议。

第二条(定义) 本协议项下"保密信息"是指:
(1) 商业秘密、技术资料、客户名单、财务数据;
(2) 标记为"保密"或"机密"的书面材料;
(3) 在合理情况下,接收方应当知道是保密性质的信息。

第三条(义务) 接收方应当:
(1) 仅为本协议目的使用保密信息;
(2) 不得向第三方披露,不得用于本协议之外的目的;
(3) 采取不低于其保护自身保密信息的注意义务。

第四条(例外) 下列信息不属于本协议保密信息:
(1) 已经或将公开为公众所知的信息;
(2) 接收方在收到披露前已合法持有的信息;
(3) 由第三方合法披露的信息;
(4) 经披露方书面同意公开的信息;
(5) 依据法律、司法或行政命令必须披露的信息。

第五条(期限) 本协议保密义务自签署之日起持续 5 年。即使协议终止,本条仍然有效。

第六条(违约) 违反本协议导致泄露的,违约方应当承担相应损害赔偿责任,并赔偿守约方因此产生的合理费用。

第七条(争议解决) 因本协议产生的争议,双方应友好协商解决;协商不成的,提交[___]仲裁委员会仲裁。

第八条(其他) 本协议一式两份,双方各执一份,自双方签字盖章之日起生效。"""

CONTRACT_CHECKLIST = """合同审查 12 项要点清单

1. 主体资格审查
   - 当事人是否具备签约主体资格(法人/自然人/非法人组织)
   - 法定代表人/授权代表权限是否真实有效
   - 委托代理人的授权范围是否覆盖签约权

2. 合同标的明确性
   - 标的物的名称、规格、数量、质量、技术标准是否清晰
   - 服务合同的服务内容、范围、交付物是否明确
   - 是否存在歧义或模糊表述

3. 价款与支付方式
   - 价款金额、币种、计价方式是否清楚
   - 支付节点、条件、方式是否合理
   - 是否包含税费条款,谁承担税款

4. 履行期限与地点
   - 交付时间、履行期限是否合理
   - 交付/履行地点是否明确
   - 履行方式(分期/一次性/阶段性)是否清晰

5. 验收标准
   - 验收的标准、程序、时限是否约定
   - 异议期及处理方式
   - 视为验收的条件

6. 知识产权归属
   - 委托开发/合作开发的成果归属
   - 商标、专利、著作权的使用许可范围
   - 第三方权利侵权风险声明

7. 保密条款
   - 保密信息的定义和范围
   - 保密期限(协议期内+终止后若干年)
   - 违约责任

8. 违约责任
   - 违约金/损害赔偿金计算方式
   - 是否合理(过高/过低均可能被法院调整)
   - 解除条件与解除后责任

9. 争议解决条款
   - 法院管辖 vs 仲裁(选其一,不得并行)
   - 管辖法院/仲裁机构具体名称
   - 适用法律

10. 不可抗力
    - 不可抗力的定义范围(自然灾害+政府行为+社会异常)
    - 通知义务和证明文件
    - 影响合同履行的处理

11. 合同变更与终止
    - 变更需双方书面同意
    - 单方解除条件
    - 协议解除的清算义务

12. 合规风险
    - 是否符合反垄断法
    - 是否涉及外汇/税务/海关等监管要求
    - 个人信息保护合规(隐私条款)"""

FALLACIES = """常见法律误区 Q&A

问:口头协议有法律效力吗?
答:有。除法律明文要求书面形式外,口头合同同样有效。但举证困难,纠纷时举证方很被动。建议重要事项尽量书面化。

问:借钱不打借条,只有微信聊天记录,法院会认吗?
答:认。微信聊天、转账记录、通话录音都可以作为证据。关键是能否形成完整证据链,证明借款合意+实际交付。

问:订金能退吗?
答:订金可退,定金原则上不退。"订金"无法律定义,是预付款,可退;"定金"具有担保性质,违约方丧失,适用《民法典》第 587 条定金罚则。

问:工资被拖欠,可以直接告老板吗?
答:不可以直接起诉。劳动争议必须先经劳动争议仲裁前置,对仲裁结果不服才能向法院起诉。劳动监察大队投诉是另一条平行救济路径。

问:房产证写谁的名字就归谁吗?
答:夫妻关系中不一定。婚后用夫妻共同财产购置的房产,即使只登记一方名字,通常仍是共同财产(《民法典》第1062 条),除非另有约定。婚前房产、父母明确赠与一方的不在此列。

问:消费者七天无理由退货是绝对权利吗?
答:不是。仅适用于网络/电视/电话等远程购物。下列商品除外:消费者定制、鲜活易腐、在线交付的数字商品、报纸期刊、拆封后影响人身安全/卫生的商品(如内衣)。

问:被打了直接还手,会被认定为正当防卫吗?
答:取决于具体情况。还手行为必须满足:(1) 防卫起因——存在不法侵害正在进行;(2) 防卫意图——为制止侵害,非报复;(3) 防卫限度——未明显超过必要;(4) 防卫对象——针对侵害人本人。同时打架(主动追打) ≠ 正当防卫。

问:合同里写"最终解释权归本公司所有"有效吗?
答:无效。属于格式条款中加重对方责任、排除对方权利的情形,《民法典》第 497 条明确无效。消费者有权要求按合理理解执行。"""


SAMPLES = [
    ("civil-code-chapter-1",       "gb-2021-civil-code", CIVIL_CODE_CH1),
    ("nda-template",               "standard-zh-v1",     NDA_TEMPLATE),
    ("contract-review-checklist",  "v1",                 CONTRACT_CHECKLIST),
    ("legal-fallacies",            "v1",                 FALLACIES),
]


def main():
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <agent_id>", file=sys.stderr)
        sys.exit(2)
    agent_id = sys.argv[1]

    edir = expert_dir_for(agent_id)
    print(f"agent: {agent_id}")
    print(f"expert dir: {edir}")

    chunker_inst = ch.get("paragraph")

    manifest = CorpusManifest.load(agent_id)
    print(f"existing manifest sources: {len(manifest.sources)}")

    for source_id, version, content in SAMPLES:
        chunks_dir = os.path.join(edir, "corpus", source_id)
        os.makedirs(chunks_dir, exist_ok=True)
        chunks_jsonl = os.path.join(chunks_dir, "chunks.jsonl")
        chunk_count = 0
        bytes_total = 0
        source_meta = {
            "source_id":    source_id,
            "version":      version,
            "ingested_at":  time.time(),
        }
        with open(chunks_jsonl, "w", encoding="utf-8") as f:
            for chunk in chunker_inst.chunk(content, source_meta):
                f.write(json.dumps(
                    {"text": chunk.text, "metadata": dict(chunk.metadata)},
                    ensure_ascii=False,
                ) + "\n")
                chunk_count += 1
                bytes_total += len(chunk.text.encode("utf-8"))
        entry = CorpusSourceEntry(
            source_id=source_id,
            version=version,
            chunk_count=chunk_count,
            bytes=bytes_total,
            indexed_at=time.time(),
            chunker_strategy="paragraph",
            notes=f"sample data, {chunk_count} chunks, {bytes_total} bytes",
        )
        manifest.add_source(entry)
        print(f"  + {source_id}: {chunk_count} chunks, {bytes_total} bytes")

    manifest.save()
    print(f"\nfinal manifest sources: {len(manifest.sources)}")
    print(f"total chunks: {manifest.total_chunks()}")
    print(f"total bytes: {manifest.total_bytes()}")
    print()
    print("Now ask the agent any legal question — RAG will pull from these.")


if __name__ == "__main__":
    main()
