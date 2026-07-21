import asyncio
import sys
from utils import STATE_FILE, logger
from main import main

async def ci_pipeline(stage):
    if stage == 'all':
        await main()
    else:
        logger.info(f"CI stage '{stage}' not implemented, running full pipeline.")
        await main()

if __name__ == '__main__':
    stage = sys.argv[1] if len(sys.argv) > 1 else 'all'
    asyncio.run(ci_pipeline(stage))
