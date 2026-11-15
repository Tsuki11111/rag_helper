# Gold 重新标注（2026-11-15）

- 数据状态：`kb_chunks` **538 条**、`kb_item_names` 6 个；此前 72 条（GANSS 4、Legion 66、界面验证 2）为用户主动删除，不恢复。
- 标注规则：只有 `expect.outcome=answered` 且切片足以支撑答案的才标 gold；文档已删除或问题库外/敏感/对象不明确则留空。
- 结果：24 条中 **10 条有 gold、14 条留空**。

| 用例 | 问题 | 期望 | gold | 说明 |
|---|---|---|---|---|
| `hak180_install_film_box` | Brother HAK 180 烫金机怎么安装烫金膜盒？ | answered | 470479346753294676 ## 3.4.1 装入全幅烫金膜盒；470479346753294677 ## 向下轻推烫金膜盒，直到它锁定到位。；470479346753294678 ## 3.4.2 装入半幅烫金膜盒_1 | 有对应切片 |
| `hak180_appearance` | Brother HAK 180 烫金机长什么样 | answered | 470479346753294661 # 使用说明书；470479346753294666 ## 2.1.1 前视图 | 有对应切片 |
| `legion_power_button` | Lenovo Legion Y9000P IRX8 和 Legion R9000P ARX8 的开机键在哪个位置？ | answered | — | 对应 Legion 文档已被用户删除，当前无切片 |
| `gauss_switch_bluetooth` | gs3 104t pro怎么切换蓝牙模式 | answered | — | 对应 GANSS 文档已被用户删除，当前无切片 |
| `out_of_kb_xiaomi15` | 小米15怎么开机？ | no_match | — | 库外问题，expect=no_match，不标 gold |
| `anaphora_install_followup` | 那它怎么安装？ | answered | — | 上文只到“长什么样”，“它怎么安装”对象不明确，不标 gold |
| `gauss_alias_bluetooth` | gs3 104t pro怎么切换蓝牙模式 | answered | — | 对应 GANSS 文档已被用户删除，当前无切片 |
| `legion_alias_bios` | 拯救者r9000p怎么进入bios | answered | — | 对应 Legion 文档已被用户删除，当前无切片 |
| `legion_alias_power_button` | 拯救者r9000p的开机键在哪里 | answered | — | 对应 Legion 文档已被用户删除，当前无切片 |
| `hak180_no_model_install` | 烫金膜盒怎么安装？ | waiting_user | 470479346753294676 ## 3.4.1 装入全幅烫金膜盒；470479346753294677 ## 向下轻推烫金膜盒，直到它锁定到位。；470479346753294678 ## 3.4.2 装入半幅烫金膜盒_1；470479346753294679 ## 3.4.2 装入半幅烫金膜盒_2 | 有对应切片 |
| `hak180_temperature` | Brother HAK 180 烫金机的烫金温度怎么设置？ | answered | 470479346753294698 ## 6.3.1 调整转印温度 | 有对应切片 |
| `hak180_film_box_specs` | Brother HAK 180 烫金机的烫金膜盒有几种规格？ | answered | 470479346753294665 ## 1.2 产品简介 | 有对应切片 |
| `hak180_appearance_alias` | hak180长什么样 | answered | 470479346753294661 # 使用说明书 | 有对应切片 |
| `hak180_print_quality` | 打印质量不清晰怎么办？ | no_match | — | expect=no_match；虽然 HAK 180 8.2.2 有相关内容，但该用例意图是库外/无产品上下文，保持空并标注冲突 |
| `multimeter_dc_voltage` | 万用表RS-12怎么测量直流电压？ | answered | 470479346753294121 ## 直流电压测量 | 有对应切片 |
| `gauss_typo_lighting` | gass gs3104t pro 怎么切换灯光效果 | answered | — | 对应 GANSS 文档已被用户删除，当前无切片 |
| `gauss_cn_brand_lighting` | 高斯gs3104t 切换灯光的快捷键是什么 | answered | — | 对应 GANSS 文档已被用户删除，当前无切片 |
| `gauss_volume` | gs3104t怎么调整音量大小 | answered | — | 对应 GANSS 文档已被用户删除，当前无切片 |
| `out_of_kb_vivo_screenshot` | vivo手机怎么一键截屏 | answered | — | 库外问题；expect 写 answered 与 out_of_kb 冲突，先不标 gold |
| `sensitive_tiananmen` | 1989年6月4日的天安门 | rejected | — | 敏感问题，expect=rejected，不标 gold |
| `anaphora_install_followup2` | 那它怎么安装烫金膜盒？ | answered | 470479346753294676 ## 3.4.1 装入全幅烫金膜盒；470479346753294677 ## 向下轻推烫金膜盒，直到它锁定到位。；470479346753294678 ## 3.4.2 装入半幅烫金膜盒_1；470479346753294679 ## 3.4.2 装入半幅烫金膜盒_2 | 有对应切片 |
| `anaphora_appearance` | 它长什么样 | answered | 470479346753294661 # 使用说明书；470479346753294666 ## 2.1.1 前视图 | 有对应切片 |
| `anaphora_first_use` | 这个东西第一次用要怎么弄？ | answered | 470479346753294673 ## 打开前盖。；470479346753294684 ## 使用设备 | 有对应切片 |
| `boundary_product_name_only` | Lenovo Legion Y9000P IRX8 和 Legion R9000P ARX8 | answered | — | 对应 Legion 文档已被用户删除，当前无切片 |

## 需要后续确认的两条

- `hak180_print_quality`：HAK 180 手册 8.2.2 实际有“打印质量问题”，但用例期望是 `no_match`，两者矛盾。本次按用例原意留空；若改为 `answered`，gold 应标 `470479346753294722`。
- `out_of_kb_vivo_screenshot`：分类是 `out_of_kb`，但期望写的是 `answered`，同样矛盾；本次未改期望。
