import uvicorn
try:
    from .config import load
    from .service import app
except ImportError:
    from config import load
    from service import app

if __name__ == '__main__':
    settings=load(); uvicorn.run(app, host='0.0.0.0', port=settings.port)
