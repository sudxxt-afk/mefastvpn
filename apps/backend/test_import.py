import asyncio
from app.services.facade import SpeedyFacadeService

async def main():
    service = SpeedyFacadeService()
    print("Facade initialized")

if __name__ == "__main__":
    asyncio.run(main())
