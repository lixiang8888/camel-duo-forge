"""测试共用的假数据。

**刻意不调用 `forge` 来造蓝图**：那要花两次模型调用，而且每跑一次结果都不一样，
测试就会变得不确定。人格和议题在这里手写死，测的是「拿到蓝图的那些模块」——
`authoring` 自己的测试在 test_authoring.py 里单独来。
"""

from __future__ import annotations

import pytest

from duo_forge.blueprint import Blueprint
from duo_forge.backends import ScriptedBackend
from duo_forge.personas import Persona
from duo_forge.purpose import Purpose
from duo_forge.topics import Topic


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


@pytest.fixture
def purpose() -> Purpose:
    return Purpose(
        text="给刚上班、每天坐十小时的程序员写一份颈椎保养手册，要能真的照做。",
        title="程序员颈椎保养手册",
    )


@pytest.fixture
def personas() -> tuple[Persona, Persona]:
    return (
        Persona(
            name="康复师",
            title="三甲医院康复科物理治疗师",
            stance="什么是对的",
            system_message="【人格】\n只认证据和量化的角度。\n\n【专业立场】\n先评估再干预。\n\n【你的盲区，不要掩饰】\n方案偏理想，不太管工位条件。",
        ),
        Persona(
            name="老张",
            title="带过两百个程序员的健身教练",
            stance="他到底做不做得到",
            system_message="【人格】\n务实、接地气。\n\n【专业立场】\n能坚持的六十分胜过两周的一百分。\n\n【你的盲区，不要掩饰】\n容易为了做得到而放松标准。",
        ),
    )


@pytest.fixture
def topics() -> tuple[Topic, ...]:
    return (
        Topic(
            key="baseline",
            section_title="一、先定基准：每天到底要做多少",
            brief="议题 1：最低有效剂量是多少，低于多少就等于没做。",
            goal="给出一个不用器械、在工位上就能完成的每日基准。",
            opening="【结论先行】我的方案是每小时起身两分钟，一天累积二十分钟。",
        ),
        Topic(
            key="reality",
            section_title="二、真实场景：忙起来根本想不起来怎么办",
            brief="议题 2：连续开会、赶版本的时候，这套东西怎么打折。",
            goal="给出至少三个真实场景下的具体兜底动作。",
            opening="【结论先行】打折的原则是：先保频率，再保强度。",
        ),
    )


@pytest.fixture
def blueprint(purpose, personas, topics) -> Blueprint:
    return Blueprint(
        purpose=purpose,
        title=purpose.title,
        slug="neck-care",
        personas=personas,
        topics=topics,
        origin="test",
    )
