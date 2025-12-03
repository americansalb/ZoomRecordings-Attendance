from fastapi import APIRouter, HTTPException
import logging

from services.zoom_service import zoom_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_accounts():
    """
    List all configured Zoom accounts

    Returns list of accounts with their IDs and names
    """
    try:
        accounts = zoom_service.get_accounts()
        logger.info(f"GET /api/accounts - Returning {len(accounts)} account(s)")

        return {
            "accounts": accounts,
            "total": len(accounts)
        }

    except Exception as e:
        logger.error(f"GET /api/accounts - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
