# 组织模块 P0 改动设计

## 1. 文档状态

- 状态：Draft
- 适用范围：Core 组织与成员生命周期，以及 Core、Knowledge、RAG 之间的授权联动
- 目标版本：组织知识最小可用流程
- P0 原则：先保证成员进入、使用、退出和权限撤销闭环可靠，再扩展多组织、审批策略和自定义角色

## 2. P0 目标

P0 必须完成以下闭环：

```text
受邀加入组织
  -> 成为有效成员
  -> 获得符合组织策略的知识权限
  -> 暂停、移除或主动退出
  -> 组织访问权限立即失效
  -> OpenFGA 和知识索引完成清理或重新同步
```

本阶段重点不是增加更多角色，而是解决以下问题：

1. 成员状态目前没有完整业务流转。
2. 永久退出后旧角色可能被重新激活。
3. 成员加入、退出或暂停后，已有知识 ACL 缺少可靠的重同步机制。
4. 退出时没有检查群聊采集责任，可能造成采集中断。
5. 退出后知识、附件和组织问答缺少明确且可验证的权限边界。
6. 组织能力没有统一接口，前端只能根据角色自行判断按钮权限。

## 3. P0 边界

### 3.1 本阶段实现

- 邀请加入组织。
- 普通成员直接退出组织。
- Owner 转让。
- 最后一个 Owner 保护。
- 管理员暂停成员。
- 管理员永久移除成员。
- Owner 或管理员恢复被暂停成员。
- 退出前的采集任务和成员责任检查。
- 永久退出时撤销全部管理角色。
- 成员变化触发知识 ACL 清理和重新同步。
- 组织能力查询接口和完整审计。

### 3.2 本阶段不做

- 一个用户同时加入多个有效组织。
- 组织开放注册或按邮箱域自动加入。
- 退出申请审批流。
- 组织删除或解散的完整数据清理流程。
- 自定义角色和任意权限组合。
- 邀请链接绑定邮箱。
- 临时访问权限的审批 UI。
- 组织层级、SSO、SCIM 和配额。

### 3.3 固定假设

1. P0 继续采用单组织模型：一个用户同一时间最多有一个 `active`、`suspended` 或 `leaving` 组织关系。
2. 当前唯一索引 `organization_memberships_one_active_org_uq` 保留。
3. 加入组织只能通过邀请完成。
4. 邀请由 `owner` 或 `membership_approver` 发起，创建邀请本身即为审批。
5. 普通成员主动退出不需要组织审批。
6. `leaving` 状态为未来审批模式预留，P0 不进入该状态。
7. 组织数据保留优先于个人退出便利性，退出不会删除组织已经获得的知识。

## 4. 当前实现缺口

| 能力 | 当前状态 | P0 缺口 |
| --- | --- | --- |
| 创建组织 | 已完成 | 无 |
| 邀请和接受邀请 | 已完成 | 缺少加入后的 ACL 重同步和完整生命周期审计 |
| 成员列表 | 已完成 | 包含暂停和待退出成员，缺少明确状态展示 |
| 固定管理角色 | 已完成 | 缺少转让、移除、暂停和退出时的角色清理 |
| 最后 Owner 保护 | 撤销角色时已有 | 退出、移除和暂停接口尚未实现 |
| 成员退出 | 数据表有字段 | 无业务接口和状态流转 |
| 成员暂停和恢复 | 数据表有字段 | 无业务接口 |
| 成员移除 | 无 | 无接口、无交接规则 |
| Owner 转让 | 无 | 最后一个 Owner 无法安全退出 |
| 采集任务交接检查 | 无 | 可能删除唯一主采集者 |
| 成员变化触发 ACL | 无 | 新成员可能看不到已有知识，退出成员关系可能残留 |
| 组织能力查询 | 无 | 前端按角色硬编码权限 |
| 审计查询 | 表结构已有 | 无查询接口，部分操作未写完整上下文 |

## 5. 成员状态机

### 5.1 状态定义

| 状态 | 含义 | 有效组织权限 |
| --- | --- | --- |
| `active` | 正常成员 | 有 |
| `suspended` | 被管理员暂停 | 无 |
| `leaving` | 未来用于退出审批，P0 不进入 | 无 |
| `left` | 已永久退出或被移除 | 无 |

### 5.2 状态转换

| 当前状态 | 操作 | 目标状态 | 角色处理 |
| --- | --- | --- | --- |
| 无成员关系 | 接受邀请 | `active` | 仅普通成员能力，无管理角色 |
| `left` | 再次接受邀请 | `active` | 清除全部历史管理角色 |
| `active` | Owner 暂停成员 | `suspended` | 保留角色，但权限归零 |
| `suspended` | Owner 恢复成员 | `active` | 恢复原角色 |
| `active` | 普通成员退出 | `left` | 撤销全部管理角色 |
| `suspended` | Owner 永久移除 | `left` | 撤销全部管理角色 |
| `active` | Owner 永久移除 | `left` | 撤销全部管理角色 |

### 5.3 Owner 约束

1. 组织始终至少保留一个有效 Owner。
2. 最后一个 Owner 不能退出、不能被移除、不能被暂停。
3. 最后一个 Owner 必须先转让 Owner 给其他 `active` 成员。
4. 如果未来实现组织解散，解散由最后一个 Owner 发起，但解散不属于本次 P0。
5. Owner 转让必须记录原 Owner、目标 Owner、操作者和时间。

## 6. 进入组织设计

### 6.1 审批策略

P0 采用“邀请即审批”：

- 邀请只能由有效 `owner` 或 `membership_approver` 创建。
- 接受邀请时不再要求第二轮审批。
- 邀请链接需要记录创建人、接受人、创建时间、接受时间和过期时间。
- 邀请链接可以转发，接受时必须以当前登录用户身份完成。

### 6.2 接受邀请流程

接受邀请必须在单个数据库事务中完成：

1. 锁定邀请记录。
2. 校验邀请状态为 `pending`。
3. 校验邀请尚未过期。
4. 校验组织状态为 `active`。
5. 校验用户没有其他有效组织关系。
6. 如果用户没有该组织成员关系，创建新的 membership。
7. 如果用户存在 `left` membership，恢复该记录为 `active`。
8. 清除 `exit_*`、`left_at`、`suspended_at` 等旧状态字段。
9. 确保用户只拥有普通成员能力，不恢复历史管理角色。
10. 将邀请标记为 `accepted`。
11. 写入 `organization.invitation_accepted` 审计日志。
12. 写入 `organization.membership.activated` outbox 事件。
13. 提交事务。

### 6.3 加入后的知识权限

进入组织不等于自动获得全部历史知识。权限规则如下：

1. 组织公开知识对全部 `active` 成员可见。
2. 群聊知识要求用户同时满足：
   - 当前为组织 `active` 成员。
   - 已完成外部身份到内部用户的映射。
   - 是该群聊的有效参与者，或拥有该资源的额外授权。
3. 敏感附件仍需额外查看或下载授权。
4. 历史问答只有在完整满足组织成员和资源授权条件时才可访问。

### 6.4 幂等要求

- 同一个邀请只能成功接受一次。
- 重复接受已过期邀请返回 `INVITATION_INVALID`。
- 用户已经在有效组织中时返回 `ORGANIZATION_ALREADY_JOINED`。
- 同一个成员激活事件可以被 Knowledge 重复消费，但不能重复增加关系。

## 7. 退出组织设计

### 7.1 审批策略

P0 不为普通成员设置退出审批。

原因：

- 强制审批会让用户因无法获得管理员操作而永久滞留在组织中。
- 离职、岗位变更等场景需要用户能够主动退出。
- 组织对数据的控制通过知识归属、访问撤销和保留策略实现，而不是阻止用户离开。

如果后续企业版本需要退出审批，可以通过组织策略开启，并进入 `leaving` 状态。该模式不能作为 P0 默认行为。

### 7.2 退出前置检查

新增退出 preflight，接口返回阻断项、警告项和需要用户执行的下一步操作。

硬阻断项：

1. 用户是最后一个有效 Owner。
2. 用户是某个 `active` 群聊的唯一有效采集者。
3. 用户是某个群聊的创建人，并且该群聊没有其他有效成员可以管理采集任务。
4. 用户仍存在不可自动转移的主采集者责任。

警告项：

1. 退出后无法访问原组织的知识、附件、群聊消息和组织问答。
2. 已经共享到组织的私人知识继续归属组织，不随成员退出删除。
3. 已下载内容无法追回。
4. 个人知识库、私人聊天和个人文件不受影响。
5. 建议退出前导出仍然需要的数据，但导出不作为强制阻断。

### 7.3 采集群聊规则

采集责任按以下规则处理：

| 用户角色 | 行为 |
| --- | --- |
| 唯一有效主采集者 | 阻断退出，必须先转让、暂停或解除采集 |
| 主采集者且存在其他有效采集者 | 允许先转让主采集者，再退出 |
| 补充采集者 | 退出时自动移除其采集者关系 |
| 个人 OAuth 或微信连接器所有者 | 保留个人连接器，但清除旧组织的默认绑定和采集任务 |

如果 P0 暂不实现主采集者转让，则唯一采集者退出前必须先暂停或解除对应群聊。

### 7.4 正式退出流程

退出事务必须完成：

1. 锁定 membership。
2. 校验 membership 为 `active` 或 `suspended`。
3. 校验最后一个 Owner 约束。
4. 校验 Knowledge preflight 无阻断项。
5. 将状态改为 `left`。
6. 写入 `left_at`。
7. 记录退出原因。
8. 撤销全部 `membership_roles`。
9. 取消该用户全部 pending 访问申请。
10. 撤销该用户获得的临时查看和下载授权。
11. 写入 `organization.member_left` 或 `organization.member_removed` 审计日志。
12. 写入 `organization.membership.deactivated` outbox 事件。
13. 提交事务。

### 7.5 退出后的访问边界

退出成功后必须立即失去以下能力：

- 查看组织知识库和组织文件。
- 查看组织群聊消息和附件。
- 搜索组织知识。
- 使用组织知识进行 RAG 检索。
- 继续组织 QA 会话或查看历史组织问答。
- 通过群聊参与者身份绕过组织成员检查。

最终授权条件必须是：

```text
当前 active 组织成员
  AND 目标资源允许该成员或群聊关系访问
  AND 敏感内容所需的额外授权
```

不能仅因为用户在外部群聊中仍然存在，就继续允许访问组织知识。

已经下载的文件无法撤回，但所有后续在线访问必须被拒绝。

## 8. 管理员暂停和移除

### 8.1 暂停成员

- 仅 Owner 可以暂停成员。
- 暂停后 membership 状态为 `suspended`。
- `EffectivePermissions` 返回空权限。
- 保留角色，便于恢复。
- 暂停成员仍然占用当前单组织名额。
- 写入 `organization.member_suspended` 审计日志。
- 触发组织授权缓存失效。

### 8.2 恢复成员

- 仅 Owner 可以恢复成员。
- 只允许 `suspended` 恢复为 `active`。
- 恢复后原角色重新生效。
- 写入 `organization.member_reactivated` 审计日志。
- 如需重新计算知识 ACL，写 `organization.membership.activated` 事件。

### 8.3 永久移除成员

- 永久移除逻辑与主动退出共用同一套 offboarding 流程。
- 必须撤销全部管理角色。
- 必须清理采集责任和临时授权。
- 必须触发知识 ACL 重新同步。
- 不允许移除最后一个 Owner。

## 9. 跨服务 ACL 联动

### 9.1 权威规则

Core 是组织成员状态的唯一权威来源。

Knowledge 和 RAG 可以缓存授权结果，但不能把缓存作为长期授权依据。所有组织范围请求必须能够重新验证用户当前是否为有效成员。

### 9.2 Core outbox 事件

P0 建议增加 IAM outbox，事件类型包括：

- `organization.membership.activated`
- `organization.membership.deactivated`
- `organization.membership.suspended`
- `organization.membership.reactivated`
- `organization.role_changed`

事件建议载荷：

```json
{
  "event_id": "uuid",
  "event_type": "organization.membership.deactivated",
  "organization_id": "uuid",
  "user_id": "uuid",
  "membership_id": "uuid",
  "membership_version": 12,
  "occurred_at": "RFC3339"
}
```

### 9.3 Knowledge 消费行为

成员变为有效时：

1. 重新评估组织公开知识的可见范围。
2. 对适用的组织知识生成 `permission.sync.requested`。
3. 对新成员不自动授予其不属于的群聊知识。

成员退出、暂停或被移除时：

1. 停止该用户关联的组织采集任务。
2. 移除该用户的 collector 关系。
3. 取消或失效其 pending 访问申请。
4. 对受影响知识生成权限重同步任务。
5. 清理不再有效的共享授权。

### 9.4 OpenFGA 关系处理

1. 退出时删除 `user:<id> member organization:<id>` 关系。
2. 即使用户仍是 `conversation_group` participant，也不能单独获得群聊知识访问权。
3. `conversation_group.member` 必须继续要求组织成员关系。
4. 对于写入失败的清理任务，必须有重试和补偿机制。

### 9.5 RAG 授权缓存

P0 需要防止退出后缓存继续允许访问。

可选方案：

1. 将 `membership_version` 加入组织授权 scope，成员状态变化后版本递增。
2. RAG 缓存 key 包含 `membership_version`。
3. 如果暂不实现版本号，则组织 scope 缓存 TTL 必须缩短，并在退出事件后主动清理。

最终要求是：退出完成后，新请求不能再通过旧授权快照访问组织知识。

## 10. HTTP 接口草案

### 10.1 用户接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/organizations/:organization_id/membership/exit-preflight` | 查询退出前阻断项和警告 |
| `POST` | `/organizations/:organization_id/leave` | 当前用户退出组织 |
| `GET` | `/organizations/:organization_id/capabilities` | 查询当前用户可执行操作 |
| `POST` | `/organizations/:organization_id/transfer-owner` | 转让 Owner |

### 10.2 Owner 管理接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/organizations/:organization_id/members/:user_id/suspend` | 暂停成员 |
| `POST` | `/organizations/:organization_id/members/:user_id/reactivate` | 恢复成员 |
| `DELETE` | `/organizations/:organization_id/members/:user_id` | 永久移除成员 |

### 10.3 内部接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/internal/organizations/:organization_id/members/:user_id/exit-preflight` | Knowledge 提供采集任务阻断信息 |
| `POST` | `/internal/organizations/:organization_id/members/:user_id/offboard` | 可选补偿接口，强制清理残留责任 |

内部接口必须校验服务令牌和 `X-Caller-Service`。

## 11. 权限和审计改动

### 11.1 新增权限

建议新增以下权限码：

- `organization.member.suspend`
- `organization.member.remove`
- `organization.owner.transfer`
- `organization.audit.read`

现有角色映射：

- `owner`：拥有全部组织权限。
- `membership_approver`：邀请和撤销邀请。
- `information_admin`：管理组织信息资源和后续访问审批。
- 普通成员：基础成员能力，不拥有成员管理权限。

### 11.2 审计事件

至少记录：

- `organization.member_suspended`
- `organization.member_reactivated`
- `organization.member_left`
- `organization.member_removed`
- `organization.owner_transferred`
- `organization.role_granted`
- `organization.role_revoked`
- `organization.invitation_created`
- `organization.invitation_accepted`
- `organization.invitation_revoked`

审计记录应包含操作者、目标用户、组织、请求 ID、结果和必要详情。

## 12. 错误语义

| 错误码 | 场景 |
| --- | --- |
| `ORG_MEMBERSHIP_REQUIRED` | 当前用户不是有效组织成员 |
| `ORG_FORBIDDEN` | 当前成员没有所需权限 |
| `LAST_OWNER_REQUIRED` | 操作会导致组织没有 Owner |
| `ORG_EXIT_BLOCKED` | 退出 preflight 存在硬阻断 |
| `ORG_COLLECTOR_TRANSFER_REQUIRED` | 必须先转让、暂停或解除群聊采集 |
| `ORG_OWNER_TRANSFER_REQUIRED` | 最后一个 Owner 必须先转让 |
| `ORG_MEMBER_STATE_INVALID` | 当前成员状态不允许执行该操作 |
| `INVITATION_INVALID` | 邀请无效、过期或已使用 |
| `ORGANIZATION_ALREADY_JOINED` | 用户已经属于有效组织 |

## 13. 测试和验收标准

### 13.1 单元测试

- 普通成员可以直接退出。
- 最后一个 Owner 不能退出、暂停或移除。
- Owner 转让成功后原 Owner 可以退出。
- 永久退出后全部管理角色被撤销。
- 重新加入不会恢复历史管理角色。
- 暂停成员没有有效权限。
- 恢复成员后原角色重新生效。

### 13.2 集成测试

- 邀请接受和成员激活在同一事务内完成。
- 同一邀请并发接受只有一次成功。
- 退出时 pending 访问申请被取消。
- 退出时临时查看和下载授权被撤销。
- 唯一采集者退出被阻断。
- 补充采集者退出后采集关系被移除。
- 唯一主采集者转让后可以退出。

### 13.3 跨服务测试

- 成员加入后，适用的组织公开知识可以被同步。
- 成员退出后，组织知识、群聊知识和问答立即拒绝访问。
- 用户仍存在于外部群聊时，也不能访问原组织知识。
- OpenFGA 关系重同步失败时任务可以重试。
- RAG 授权缓存不会在成员退出后继续授权。

### 13.4 P0 完成定义

满足以下条件才视为 P0 完成：

1. 最后一个 Owner 无法直接离开组织。
2. 唯一采集者无法在群聊继续采集时退出。
3. 退出成功后立即失去组织知识、群聊知识和组织问答权限。
4. 退出后重新加入不会继承旧的管理角色。
5. 成员加入、退出和暂停可以触发知识 ACL 的可靠重同步。
6. 所有关键操作具有可查询的审计记录。
7. 前端通过能力接口控制按钮，不自行推断权限。

## 14. 实施顺序

建议按以下顺序落地：

1. 完成成员状态机、Owner 转让、暂停、恢复和移除。
2. 完成退出 preflight 和采集责任检查。
3. 完成 Core 审计、能力接口和 IAM outbox。
4. 完成退出时角色、申请和临时授权清理。
5. 完成 Knowledge 消费事件和知识 ACL 重同步。
6. 完成 RAG 成员状态复核和授权缓存失效。
7. 完成前端退出流程、阻断提示和确认页面。
8. 补齐单元测试、PostgreSQL 集成测试和跨服务测试。

## 15. 后续阶段

以下能力不阻塞 P0，但应保留设计空间：

- 多组织与组织切换。
- 组织级退出审批策略。
- 邀请绑定邮箱或指定用户。
- 组织冻结、解散和数据保留期。
- 自定义角色和临时权限。
- 审计日志查询和导出 UI。
- 组织配额、SSO、SCIM 和组织层级。
