bind = "0.0.0.0:5000"
# 同步进度状态存在进程内存（sync_progress 字典），
# 必须用单 worker + 多 threads，否则前端轮询会命中不同 worker 拿不到进度。
# 同步是 IO 密集型（飞书 API），threads 完全够用。
workers = 1
threads = 4
timeout = 1800
accesslog = "-"
errorlog = "-"
