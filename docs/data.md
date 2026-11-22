# 资料说明

各集合保存在 `data/`，JSON 对象的 `fields` 为字段列表，`key` 为集合主键，`rows` 为记录数组。各记录值均为字符串，空串含义以各集合的说明为准。

## sessions

每次接洽的主家庭、渠道和实际展示项目；此列表不保证全站目录齐全。

主键为 `session`，字段为 `session`、`household`、`day`、`site`、`channel`、`displayed`。

## line_items

原始办理项目、实际服务对象、确认状态与套餐组成来源。

主键为 `id`，字段为 `id`、`session`、`household`、`alias`、`observed_id`、`state`、`parent`。

## aliases

登记的映射或候选。记录 ambiguous 时仍待人工确认，approved 仅在注明范围内有效。

主键为 `id`，字段为 `id`、`alias`、`site`、`start`、`end`、`canonical`、`state`。

## catalog

服务代码的真实有效期、办理资格、单办条件、互斥项和先办项。基本资格 general 包含所有家庭。

主键为 `id`，字段为 `id`、`code`、`since`、`until`、`eligibility`、`independent`、`excludes`、`requires`。

## population

柜台用于当前资格核查的已确认范围。

主键为 `household`，字段为 `household`、`group`。

## advice

历史已经提供给家庭的建议和受理状态。成员依据当时采信的来源。

主键为 `id`，字段为 `id`、`household`、`site`、`services`、`source`、`at`、`accepted`。
