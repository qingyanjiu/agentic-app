# Java MCP Sidecar 接口清单与 Mock 返回格式

> 供 Java 侧车（`host.docker.internal:2222`）实现 mock 数据使用。
> 依据：Python 侧各域 graph 的 `_JAVA_TOOL_MAP`（工具名与入参）、`tests/conftest.py` 的 `DOMAIN_TOOL_RESULTS`（已被测试验证可走通流程的返回样例）、`docs/项目结构与接口文档.md`（平台 HTTP 接口字段口径）。
> 更新日期：2026-09-21 · 分支：main

---

## 一、对接总览

1. **传输方式**：13 个端点全部为 MCP `streamable_http`，端点路径与 Python 侧配置一一对应（见各节）。
2. **工具名必须完全一致（含冒号前缀）**：Python 侧按全名取工具，如 `person_status:getTodayPersonnelAffairs`。前缀即各节标注的「命名空间」。
3. **返回形式**：MCP 工具的文本内容是一个 **JSON 字符串**。Python 侧兼容两种包装（标准 MCP 的 `[{type:"text",text:"..."}]` 或纯字符串均可）。
4. **通用返回包装**（与平台 HTTP 接口一致的简化口径）：
   - 列表类：`{"code":200,"total":N,"rows":[...]}`（total 可省略）
   - 对象类：`{"code":200,"data":{...}}`
   - `code:200` 表示成功。
5. **入参基本可忽略**：大多数工具 Python 侧传 `{}`；带参工具见各表「入参」列，**建议在 Java 侧把这些参数声明为可选**，空参调用不能报错。
6. **时间参数格式**：`startTime`/`endTime` 为 ISO 串（如 `2026-09-01T00:00:00`）；`date` 为 `yyyy-MM-dd` 或 `yyyy-MM`；`type` 固定传 `day`。
7. **容错度高，但「加粗字段」例外**：绝大多数返回直接喂给 LLM 组织回答，字段名不严格校验；只有 §三 列出的场景 Python 会**程序化解析字段**，这些字段名必须精确。
8. **可照抄的参考实现**：`mcp_servers/fire_server.py`、`perimeter_server.py`、`inspection_server.py` 是三个字段口径一致的 Python stdio mock，数据结构可直接抄。

---

## 二、端点与工具清单

### 1. /mcp/personStatus 人员态势（前缀 `person_status:`）

| 工具名 | 入参 | 返回示例 |
|---|---|---|
| `person_status:getTodayPersonnelAffairs` | `{}` | `"实时在园人数：12人；今日进入人数：30人；今日离开人数：25人。"`（纯文本；或 JSON `{realtimeCount,enterCount,leaveCount}` 均可） |
| `person_status:getTodayPersonnelFlow` | `{"type":"day"}` | `{"code":200,"rows":[{"time":"09:00","enter":5,"leave":3}]}`（按小时的进入/离开人数曲线） |
| `person_status:getPersonnelStructure` | `{}` | `{"code":200,"rows":[{"dept":"中通服","count":80}]}`（部门人员构成饼图） |

### 2. /mcp/securityStatus 安防态势（前缀 `security:`）

| 工具名 | 入参 | 返回示例 |
|---|---|---|
| `security:getSecurityIndex` | `{}` | `{"code":200,"rows":[{"index":92.5}]}`（可加 `indicators:[{name,value,unit}]` 分项指标） |
| `security:getSecurityAlarmList` | `{}` | `{"code":200,"total":1,"rows":[{"alarmName":"周界入侵","areaName":"大门口"}]}` |
| `security:getPatrolMission` | `{}` | `{"code":200,"total":2,"rows":[{"missionName":"上午巡查"}]}` |
| `security:getDeviceDetail` | `{}` | 设备档案明细列表 `{"code":200,"rows":[...]}` |
| `security:getAiInspectionEvents` | `{"startTime":"...","endTime":"..."}` | AI 巡查事件列表 `{"code":200,"rows":[{类型/等级/图片/位置字段}]}` |
| `security:getAiAlertSituation` | `{"startTime":"...","endTime":"..."}` | 各 AI 告警类别数量与占比 `{"code":200,"rows":[{"类别":"dahua","count":N,"percentage":P}]}` 或 `Record<类别,{count,percentage}>` |
| `security:getInspectionTrend` | `{"date":"2026-09-21"}` | `{"code":200,"category":["09-15","09-16"],"oneData":[10,8],"twoData":[1,2]}`（正常/异常两条趋势线） |
| `security:getAlarmView` | `{}` | `{"code":200,"todayAlarmCount":5,"totalAlarmCount":320,"algorithmTypeCount":8,"accuracy":95.2}` |
| `security:getAlertSituation` | `{"startTime":"...","endTime":"..."}` | `{"code":200,"rows":[{"date":"2026-09-14","count":6}]}`（近 7/30 天趋势） |
| `security:getAlarmListWithType` | `{"startTime":"...","endTime":"..."}` | `{"code":200,"rows":[{"type":"管理预警","count":4},{"type":"环境预警","count":2}]}` |

### 3. /mcp/canteen 食堂管理（前缀 `canteen:`）

三个工具入参相同：`{"startTime":"...","endTime":"..."}`，可带可选 `meal`（早/午/晚餐）。

| 工具名 | 返回示例 |
|---|---|
| `canteen:getDiningCount` | `{"code":200,"total":1,"rows":[{"date":"2026-09-10","count":321}]}` |
| `canteen:getDishPopularity` | `{"code":200,"total":2,"rows":[{"dishName":"红烧肉","score":98}]}`（平台口径另有 `price,soldCount,likes,positiveReview`） |
| `canteen:getWeekMenu` | `{"code":200,"data":[{"day":"周一","breakfast":[{"dishName":"番茄炒蛋","price":5,"unit":"份"}],"lunch":[...],"dinner":[...]}]}` —— **整周菜谱走 `data` 包装而非 `rows`，Python 侧会解析**，见 §三 |

### 4. /mcp/vehicleStatus 车辆态势（前缀 `vehicle_status:`）

全部无参（后端只支持今日数据）。

| 工具名 | 返回示例 |
|---|---|
| `vehicle_status:getParkingSpace` | `{"code":200,"rows":[{"total":200,"used":120,"remain":80}]}` |
| `vehicle_status:getTrafficVolume` | `{"code":200,"rows":[{"time":"09:00","in":10,"out":8}]}`（按小时进出车流量） |
| `vehicle_status:getParkingStructure` | `{"code":200,"rows":[{"name":"临时停车","value":60}]}`（停车结构饼图） |
| `vehicle_status:getCarStatistic` | `{"code":200,"data":{"total":40,"used":12,"inCompany":25,"outCompany":3}}`（公车统计） |
| `vehicle_status:getParkingRank` | `{"code":200,"data":{"overtimeStayRate":5.2,"avgParkingHours":2.5,"avgParkingMinutes":150,"tableData":[{"carId":"浙A12345","parkTime":"3小时20分"}]}}` |

### 5. /mcp/information 信息发布（前缀 `information:`）

六个工具入参相同：`{"startTime":"...","endTime":"..."}`。

| 工具名 | 返回示例 |
|---|---|
| `information:getInfoView` | `{"code":200,"statisticList":[{"name":"发布中","value":30}],"deviceList":[{"name":"大堂信息屏","status":"在线","online":1}],"typeData":[{"name":"横屏","value":12}]}` |
| `information:getBroadcastView` | `{"code":200,"rows":[{"name":"在线","value":20},{"name":"离线","value":2}]}` |
| `information:getTaskTrend` | `{"code":200,"xData":["09-15","09-16"],"data":{"oneData":[15,18],"twoData":[14,17]}}`（任务执行两条趋势线） |
| `information:getProgramCount` | `{"code":200,"count":[3,5,2],"xData":["07","08","09"]}`（节目数量趋势） |
| `information:getInfoPublishiEquip` | 同 `getInfoView` 口径（信息发布设备明细） |
| `information:getBroadcastEquip` | `{"code":200,"rows":[{"name":"北门广播","status":"在线","taskName":"午间广播"}]}` |

### 6. /mcp/energyStatus 能源态势（前缀 `energy:`）

全部无参。

| 工具名 | 返回示例 |
|---|---|
| `energy:getOverallEnergyConsu` | `{"code":200,"rows":[{"electricity":12000,"water":3000}]}`（更真实口径：`{"code":200,"data":{"electricity":{"annual":12000,"today":320},"water":{"annual":3000,"today":80}}}`，两种 Python 侧都能处理） |
| `energy:getMeteringEquipment` | `{"code":200,"data":{"electricityMeterCount":42,"waterMeterCount":18}}` |
| `energy:getElectricityUsageRanking` | `{"code":200,"rows":[{"area":"A栋","value":500}]}` |
| `energy:getWaterUsageRanking` | `{"code":200,"rows":[{"area":"A栋","value":200}]}` |
| `energy:getDeviceStatus` | `{"code":200,"data":{"onlineCount":56,"offlineCount":4}}` |
| `energy:getRealtimeElectricityUsage` | `{"code":200,"rows":[{"timeLabel":"08:00","todayValue":120,"yesterdayValue":110}]}`（今日 vs 昨日曲线） |
| `energy:getRealtimeWaterUsage` | 同上（用水口径） |

### 7. /mcp/meetingStatus 会议管理（前缀 `meeting:`）

| 工具名 | 入参 | 返回示例 |
|---|---|---|
| `meeting:getMeetingStatistics` | `{}` | `{"code":200,"rows":[{"month":"2026-09","count":42,"avgDuration":55}]}` |
| `meeting:getHighFrequencyMeetingRooms` | `{}` | `{"code":200,"rows":[{"roomName":"第一会议室","times":18}]}`（可加 `address,capacity`） |
| `meeting:getMeetingRoomOverview` | `{}` | `{"code":200,"rows":[{"roomName":"第一会议室","capacity":12}]}` |
| `meeting:getNumberOfMeetings` | `{}` | `{"code":200,"rows":[{"date":"2026-09-10","count":5}]}` |
| `meeting:getMeetingRoomStatus` | `{}` | `{"code":200,"rows":[{"roomName":"第一会议室","status":"占用"}]}` |
| `meeting:getRoomMeetListByDay` | `{"startTime":"...","endTime":"..."}` | `{"code":200,"total":1,"rows":[{"roomName":"第一会议室","meetName":"周会","startTime":"09:30","endTime":"10:30","applyUserName":"张三","meetStatus":"1"}]}`（meetStatus：0未开始/1进行中/2已结束/3已取消） |

### 8. /mcp/fire 消防态势（前缀 `fire:`）

全部无参。

| 工具名 | 返回示例 |
|---|---|
| `fire:getFireAlarmList` | `{"code":200,"total":1,"rows":[{"title":"烟感火警","alarmTypeName":"火警","assetsName":"3F-烟感-001","areaName":"A栋","alarmLevel":"高","handleStatus":"未处理","nowAlarmTime":1725926400000,"alarmReason":"测试"}]}` —— rows 字段会被 Python 压缩解析，见 §三 |
| `fire:getFireAlarmNum` | `{"code":200,"rows":[{"fireNum":2,"faultNum":1}]}`（更全口径见平台文档：`allAlarmNum,fireNum,faultNum,fireDangerNum,fireMisreportNum,deviceFaultNum,userLeaveNum` 等，可平铺在 rows[0] 里） |
| `fire:getFireAssets` | `{"code":200,"total":1,"rows":[{"assetsName":"干粉灭火器","deviceCode":"MH-001","offLineFlag":0,"faultFlag":0,"usageFlag":1,"hiddenDangerFlag":0,"dataMap":[{"monitorName":"压力","monitorValue":0.45,"unit":"MPa"}]}]}` —— rows 字段会被 Python 压缩解析，见 §三 |
| `fire:getMonthRepair` | `{"code":200,"rows":[{"month":"2026-09","repairNum":4}]}` |

### 9. /mcp/perimeter 周界态势（前缀 `perimeter:`）

全部无参。

| 工具名 | 返回示例 |
|---|---|
| `perimeter:getKeyMetrics` | `{"code":200,"data":{"defenseTotal":24,"onlineNum":22,"offlineNum":2,"todayAlarmNum":5}}` |
| `perimeter:getAreaOverview` | `{"code":200,"total":2,"rows":[{"areaName":"东侧围墙防区01","status":"布防"}]}` |
| `perimeter:getPerimeterAlarmStats` | `{"code":200,"data":{"areaStats":[{"areaName":"东侧围墙防区01","alarmNum":3}],"hourCurve":[{"hour":"08","alarmNum":1}]}}` |
| `perimeter:getAlarmOverview` | `{"code":200,"total":1,"rows":[{"alarmName":"周界入侵告警","areaName":"东侧围墙防区01","alarmLevel":"重要","handleStatus":"未处理","alarmTime":1725926400000,"alarmReason":"检测到翻越行为"}]}` —— rows 字段会被 Python 压缩解析，见 §三 |

### 10. /mcp/device 设备态势（前缀 `device:`）

| 工具名 | 入参 | 返回示例 |
|---|---|---|
| `device:getEquipClass` | `{}` | `{"code":200,"rows":[{"name":"消防设备","value":120},{"name":"空调","value":86}]}` |
| `device:getCategoryHealth` | `{}` | `{"code":200,"allDevices":[{"name":"消防设备","score":92,"onlineRate":0.98,"maintenanceRate":0.9,"lifeRate":0.85}]}` |
| `device:getMonthMaintenance` | `{"date":"2026-09"}`（可选） | `{"code":200,"xData":["2026-07","2026-08","2026-09"],"series":[{"name":"维修量","data":[5,8,3]}]}` |
| `device:getMonthRepair` | `{"date":"2026-09"}`（可选） | `{"code":200,"xData":["2026-07","2026-08","2026-09"],"series":[{"name":"报修量","data":[12,9,7]}]}` |
| `device:getStatisRegion` | `{}` | `{"code":200,"MAX":[200,180],"VALUE":[150,120],"xAxisNameMap":["A栋","B栋"]}` |
| `device:getAnfangDeviceOnlinePercentage` | `{}` | `{"code":200,"total":{"online":180,"total":200},"zhoujie":{"online":50,"total":52},"dz":{"online":60,"total":65},"mj":{"online":70,"total":72}}` |
| `device:getGbOnlinePercentage` | `{}` | `{"code":200,"online":45,"total":50}` |
| `device:getMjOnlinePercentage` | `{}` | `{"code":200,"online":70,"total":72}` |

注：设备态势的「台账/设备清单」问法会跨端点调 `/mcp/devicequery` 的 `device_query:listDevice`。

### 11. /mcp/devicequery 设备查询·资产库口径（前缀 `device_query:`）

| 工具名 | 入参 | 返回示例 |
|---|---|---|
| `device_query:listDevice` | 全部可选：`{"name":"枪机"}`（**模糊**匹配）或 `{"code":"CY-HIK-JK-001-0001"}`（**精确**匹配）或 `{"syncSource":"3"}`（按设备类型码筛）；可带 `pageSize`。Python 侧详情定位流程是：先按 name 搜、搜不到再按 code 搜 | `{"code":200,"data":{"page":{"total":2,"size":100,"pages":1,"current":1},"data":[{"id":"1001","name":"A栋枪机","code":"CY-HIK-JK-001-0001","syncSource":"3","deviceTypeName":"监控设备","status":"1","spaceId":"2001","spaceName":"园区/A栋/3楼","personInChargeName":"张三","orgName":"安防部","maintained":"1"}]}}` |
| `device_query:getDeviceDetail` | `{"id":"1001"}`（**只认列表返回的内部 id**） | `{"code":200,"data":{"id":"1001","name":"A栋枪机","code":"CY-HIK-JK-001-0001","syncSource":"3","deviceTypeName":"监控设备","status":"1","spaceId":"2001","spaceName":"园区/A栋/3楼","personInChargeName":"张三","orgName":"安防部","maintained":"1"}}` |

字段口径：
- `id`：**必须返回**，详情查询靠它定位；搜索结果解析也依赖 `data.data[].id/name/code/spaceName`。
- `status`：资产启用状态枚举 `0`停用 `1`启用 `2`维修 `3`报废（不是在线/离线）。
- `syncSource`：设备类型码（如 `3`=监控设备、`0`=门禁设备），Java 侧自定一套并保持一致即可。
- 该端点如还有状态统计/在线率/厂商透传等另外三组工具，Python 侧暂不使用，mock 可先不实现。

### 12. /mcp/overview 综合态势总览（前缀 `overview:`）

全部无参。

| 工具名 | 返回示例 |
|---|---|
| `overview:getBasicInfo` | `{"code":200,"centerArea":320,"iotDeviceCount":1200}`（园区面积万㎡、IoT 设备总数） |
| `overview:getDeviceHealth` | `{"code":200,"rows":[{"name":"消防设备","score":92,"onlineRate":{"current":98,"total":100,"percent":98},"maintenanceRate":{"percent":90},"lifeRate":{"current":85,"total":100,"percent":85}}]}` |

### 13. /mcp/inspection 孪生巡检（前缀 `inspection:`）

全部无参。

| 工具名 | 返回示例 |
|---|---|
| `inspection:getTodayInspection` | `{"code":200,"data":{"taskCount":8,"pointCount":46,"completionRate":87.5,"chartData":{"xData":["08:00","10:00"],"yData1":[2,3],"yData2":[2,2]}}}` |
| `inspection:getTodayTasks` | `{"code":200,"total":1,"rows":[{"name":"张伟","type":"日常巡检","state":"已完成","stateClass":"success","team":"巡检一班","time":"08:30"}]}` |
| `inspection:getInspectionStatistics` | `{"code":200,"data":{"avgTime":"25分钟","pointCount":320,"hazardCount":6,"chartData":[{"value":12,"name":"日常巡检"}]}}` |
| `inspection:getInspectionExecutionStatus` | `{"code":200,"data":{"xData":["张伟","李娜"],"yData1":[12,10],"yData2":[1,0]}}`（按人正常/异常两条柱） |

---

## 三、Python 侧会程序化解析的字段（必须精确）

其余返回直接喂给 LLM，字段名宽容；以下四处 Python 有解析逻辑，字段名/结构错了会退化为展示原始 JSON：

| 场景 | 必须的结构 |
|---|---|
| `canteen:getWeekMenu`（周菜谱压缩） | 顶层 `{"data":[...]}`（或直接数组），每项 `{"day":"周一","breakfast":[{"dishName","price","unit"}],"lunch":[...],"dinner":[...]}`；多余长字段（image/elementJson 等）可省 |
| `fire:getFireAssets`（台账压缩） | `{total,rows:[...]}`；行内保留字段 `assetsName/offLineFlag/faultFlag/usageFlag/hiddenDangerFlag/deviceCode`；压力/液位/电量/倾角放在 `dataMap:[{"monitorName":"压力","monitorValue":0.45,"unit":"MPa"}]` |
| `fire:getFireAlarmList` / `perimeter:getAlarmOverview`（告警压缩） | `{total,rows:[...]}`；保留字段：消防 `title/alarmTypeName/assetsName/areaName/alarmLevel/handleStatus/nowAlarmTime/alarmReason`；周界 `alarmName/areaName/alarmLevel/handleStatus/alarmTime/alarmReason`。字段名以 `Time` 结尾且为纯数字时按**毫秒时间戳**解析 |
| `device_query:listDevice`（搜索定位 + 本地过滤） | `{"data":{"page":{...},"data":[...]}}`；行内 `id`（必需）、`name`、`code`、`spaceName`（按位置筛选时本地过滤用） |

---

## 四、Java 侧自测建议

1. 每个端点先保证 `tools/list` 能列出上表全部工具名（**前缀一致**），再逐个实现 `tools/call`。
2. 空参调用 `{}、{"startTime":null,"endTime":null}` 都必须正常返回，不要因缺参报错。
3. 联调时 Python 侧日志会打印 `[MCP CALL] tool=xxx, args={...}` 与 `[MCP RAW RESULT] tool=xxx, result=...`，可直接比对入参和返回。
4. 现成参考：`mcp_servers/fire_server.py`、`perimeter_server.py`、`inspection_server.py` 三个 Python mock 的数据结构即本表口径。
