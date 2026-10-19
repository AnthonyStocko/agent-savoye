import os
import requests
import msal
from crewai.tools import tool
from dotenv import load_dotenv

load_dotenv()

TOKEN_CACHE_FILE = "ms_token_cache.bin"

def get_ms_app():
    """Initialise le client MSAL avec stockage local des jetons."""
    client_id = os.getenv("MS_CLIENT_ID", "04b07795-8ddb-461a-bbee-02f9e1bf7b46") # ID public Microsoft
    authority = "https://login.microsoftonline.com/common"
    
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE, "r") as f:
            cache.deserialize(f.read())
        
    app = msal.PublicClientApplication(
        client_id, authority=authority, token_cache=cache
    )
    return app, cache

def get_msgraph_token():
    """Récupère ou rafraîchit le jeton d'accès Microsoft Graph."""
    app, cache = get_ms_app()
    scopes = ["Calendars.ReadWrite", "User.Read"]
    
    accounts = app.get_accounts()
    result = None
    
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        
    if not result:
        print("\n=== Connexion Microsoft 365 requise ===")
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise ValueError(f"Échec de l'initialisation du flux : {flow.get('error_description')}")
            
        print(flow["message"]) # Instructions avec URL et code
        result = app.acquire_token_by_device_flow(flow)
        
        if cache.has_state_changed:
            with open(TOKEN_CACHE_FILE, "w") as f:
                f.write(cache.serialize())

    if result and "access_token" in result:
        return result["access_token"]
    else:
        raise Exception("Échec de l'obtention du jeton d'accès Microsoft.")

@tool("Planifier une réunion sur Outlook")
def create_outlook_meeting(subject: str, start_time: str, end_time: str, attendees: str) -> str:
    """
    Crée une réunion dans l'agenda Outlook.
    - start_time / end_time format ISO : '2026-09-25T10:00:00'
    - attendees : liste d'e-mails séparés par des virgules
    """
    try:
        token = get_msgraph_token()
        url = "https://graph.microsoft.com/v1.0/me/events"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        attendee_list = [{"emailAddress": {"address": email.strip()}, "type": "required"} for email in attendees.split(",")]
        
        payload = {
            "subject": subject,
            "start": {"dateTime": start_time, "timeZone": "Europe/Paris"},
            "end": {"dateTime": end_time, "timeZone": "Europe/Paris"},
            "attendees": attendee_list,
            "isOnlineMeeting": True,
            "onlineMeetingProvider": "teamsForBusiness"
        }
        
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 201:
            return f"Réunion '{subject}' planifiée avec succès sur Outlook."
        return f"Erreur lors de la création de la réunion : {response.text}"
    except Exception as e:
        return f"Erreur Outlook : {str(e)}"