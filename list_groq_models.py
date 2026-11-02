"""One-off: list the Groq models your API key currently has access to."""
import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
client = Groq(api_key=os.environ["GROQ_API_KEY"])
models = client.models.list()
for m in models.data:
    print(m.id)