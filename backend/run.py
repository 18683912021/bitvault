"""BitVault 启动入口：python run.py"""
import uvicorn

from app import config

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════╗
║  BitVault — BTC 量化交易操作系统               ║
║  UI:  http://{config.HOST}:{config.PORT}          ║
║  默认环境: OKX 模拟盘（先配置 API Key）         ║
╚══════════════════════════════════════════════╝
""")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info")
