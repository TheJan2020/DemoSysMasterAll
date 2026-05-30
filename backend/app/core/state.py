"""Persistent in-memory state. Backed by data/state.json."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

_STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "state.json"
_lock = threading.Lock()


_DEFAULT_GEMINI_MODEL = "gemini-2.0-flash-live-001"


class State:
    def __init__(self) -> None:
        self.frigate_url: Optional[str] = None
        self.homeassistant_url: Optional[str] = None
        self.homeassistant_token: Optional[str] = None
        self.gemini_api_key: Optional[str] = None
        self.gemini_model: str = _DEFAULT_GEMINI_MODEL
        # DeepSeek (OpenAI-compatible API at api.deepseek.com by default).
        # Used as an alternative vision provider in the Restaurant Kitchen
        # AI-Camera + AI-Rules pages, side-by-side with Gemini.
        self.deepseek_api_key: Optional[str] = None
        self.deepseek_base_url: str = "https://api.deepseek.com"
        # OpenRouter (OpenAI-compatible aggregator at openrouter.ai). Gives
        # a single key access to Qwen-VL, DeepSeek-VL2, Llama 3.2 Vision,
        # Pixtral, GPT-4o, Claude Haiku, and more — used as a third vision
        # provider so we can pick the cheapest/best model for the task.
        self.openrouter_api_key: Optional[str] = None
        self.openrouter_base_url: str = "https://openrouter.ai/api/v1"
        # ElevenLabs streaming TTS — when an api_key is set, the Live
        # Agent (currently restaurant only) switches Gemini Live to
        # text-only response and synthesises voice through ElevenLabs's
        # bidirectional WebSocket. Defaults are an Arabic-friendly female
        # voice on the multilingual_v2 model; both are editable from
        # Settings so you can audition voices without a code change.
        self.elevenlabs_api_key: Optional[str] = None
        self.elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"   # Sarah
        self.elevenlabs_model_id: str = "eleven_multilingual_v2"
        # Voice provider for the restaurant Live Agent — three choices:
        #   "gemini"                     — Gemini Live native audio
        #                                   (default, best phone UX).
        #   "gemini_with_elevenlabs_tts" — Gemini Live's brain + tool
        #                                   calls + transcription, but
        #                                   Gemini audio is dropped and
        #                                   ElevenLabs Streaming TTS
        #                                   speaks Gemini's text. Lets
        #                                   you use ANY ElevenLabs voice
        #                                   (incl. shared community PVCs
        #                                   like Sahl/Abdullah that CAI
        #                                   rejects) — at the cost of
        #                                   streaming-TTS prosody quirks.
        #   "elevenlabs_cai"             — ElevenLabs Conversational AI;
        #                                   their stack replaces Gemini
        #                                   entirely (STT + LLM + TTS
        #                                   bundled). Persona/KB/tools
        #                                   are auto-synced via their
        #                                   REST API; only the agent_id
        #                                   is stored here. Best voice
        #                                   quality, but voices must be
        #                                   CAI-eligible (premade or
        #                                   owned clones).
        self.voice_provider: str = "gemini"
        # Set by `cai_elevenlabs.ensure_agent()` on first create / update.
        self.elevenlabs_agent_id: Optional[str] = None
        # MQTT broker (used to subscribe to Frigate's push events)
        self.mqtt_host: Optional[str] = None
        self.mqtt_port: int = 1883
        self.mqtt_username: Optional[str] = None
        self.mqtt_password: Optional[str] = None
        self.mqtt_topic_prefix: str = "frigate"
        # Ollama (local) — optional alternative vision provider for rules.
        self.ollama_url: Optional[str] = None
        # Persistent AI-Camera rules — see services/ai_camera_engine for the
        # runtime engine that consumes these. Each entry is a plain dict; the
        # rules router owns the schema, this layer only round-trips the list.
        self.ai_camera_rules: list[dict[str, Any]] = []
        # PBX — single source of truth for "where is the SIP server". The
        # Live Assistant uses this for connectivity checks; the softphone
        # derives a default ws_url from it (wss://<host>:8089/ws) unless the
        # user has overridden ws_url on the Extension page.
        self.pbx_host: Optional[str] = None
        # SIP softphone — settings the browser-side JsSIP client uses to
        # register with the PBX. ws_url is the WebSocket endpoint (ws:// or
        # wss://). realm is the SIP domain part of the AOR; falls back to the
        # host in ws_url if blank.
        self.sip_ws_url: Optional[str] = None
        self.sip_extension: Optional[str] = None
        self.sip_password: Optional[str] = None
        self.sip_realm: Optional[str] = None
        self.sip_display_name: Optional[str] = None
        # SIP Live Assistant — backend AudioSocket service that answers
        # FreePBX-routed calls with a Gemini Live session. See
        # docs/LiveAssistantSIP.md for the full architecture.
        self.sla_enabled: bool = False
        self.sla_bind_host: str = "0.0.0.0"
        self.sla_bind_port: int = 8090
        self.sla_system_prompt: str = (
            "You are a friendly phone receptionist for a smart-home demo. "
            "Greet the caller, identify who is calling, and route the call. "
            "If the caller asks you to control devices, use the Home Assistant "
            "tools you have access to. Keep replies conversational and short."
        )
        self.sla_voice: str = "Aoede"
        self.sla_greeting: str = "Hello, you've reached the Primewave demo. How can I help?"
        self.sla_enable_ha_tools: bool = True
        self.sla_only_areas: bool = False
        self.sla_max_call_s: int = 600
        # Frigate cameras the SIP Live Assistant pipes into Gemini Live as
        # video frames during a call, so the agent can answer questions like
        # "is anyone at the front door?".
        self.sla_cameras: list[str] = []
        # SIP Live Representative — separate Gemini-Live-backed AudioSocket
        # service tailored to be Primewave's public-facing rep. Has its own
        # extension/feature-code on FreePBX (default 8888 → port 8091),
        # persistent call history, and no HA / camera capabilities by default.
        self.slr_enabled: bool = False
        self.slr_bind_host: str = "0.0.0.0"
        self.slr_bind_port: int = 8091
        self.slr_system_prompt: str = (
            "أنتِ لينا، الممثلة الرسمية لخدمة العملاء في شركة برايم ويف "
            "(Primewave). كلامك دايماً باللهجة اللبنانية، بأسلوب دافئ "
            "وودود ومحترف، مثل أي ممثل خدمة عملاء محترف بشركة كبيرة.\n"
            "\n"
            "اشتغلي كأنّك إنسان حقيقي، استعملي لهجة عفوية، ومتزعزعيش "
            "بالأسئلة. إذا الزبون سأل سؤال ما إلو علاقة بشغلنا، رجّعيه "
            "بلطف للموضوع. إذا ما عرفتي إجابة على سؤال تقني محدد، "
            "اعتذري بصدق وقولي إنّك بتحرصي يتواصل معه أحد المختصين "
            "لاحقاً — متختلقيش معلومات.\n"
            "\n"
            "خلال المكالمة، حاولي بشكل طبيعي وغير مزعج تجمعي المعلومات "
            "المطلوبة من المتصل (موجودة بالأسفل). كل ما تأكّدتي من معلومة "
            "جديدة، استدعي وظيفة `save_caller_information` لتخزينها. "
            "في إمكانك تستدعيها أكثر من مرة، كل ما يتأكد عندك شي جديد. "
            "ما تتذكري كل المعلومات لآخر المكالمة — احفظيها على دفعات."
        )
        self.slr_voice: str = "Aoede"
        self.slr_greeting: str = (
            "أهلا فيك معنا، أنا لينا من شركة برايم ويف. كيف فيني ساعدك اليوم؟"
        )
        self.slr_max_call_s: int = 900
        # ---- Clinic Demo Live Agent (cda_*) ---------------------------------
        # AudioSocket service dedicated to the Clinic vertical demo (Layla,
        # the clinic receptionist). Lives on its own port so it never clashes
        # with the admin demos' SLA (8090) or SLR (8091). Persona + KB are
        # read from data/demos/clinic/persona.txt and .../kb.txt — the Clinic
        # SPA's KB / Persona pages publish to those files via the
        # /api/demo/clinic/agent/prompt endpoint.
        self.cda_enabled: bool = False
        self.cda_bind_host: str = "0.0.0.0"
        self.cda_bind_port: int = 8092
        self.cda_voice: str = "Aoede"
        self.cda_greeting: str = (
            "السلام عليكم، عيادات برايم ميت. أنا ليلى. كيف أقدر أخدمك؟"
        )
        self.cda_max_call_s: int = 900
        self.cda_interruption_enabled: bool = True
        # When true, the agent stops speaking the moment the caller starts
        # (Gemini VAD with HIGH start-of-speech sensitivity, plus we drop the
        # output audio queue). When false, the agent finishes its turn before
        # listening.
        self.slr_interruption_enabled: bool = True
        # Free-form description of Primewave's offerings — Lena uses it to
        # answer questions about what we do, prices, capabilities, etc.
        self.slr_knowledge: str = (
            "## خدماتنا في البيت الذكي (Smart Home)\n"
            "\n"
            "### أ — الأجهزة والأنظمة\n"
            "- تحكّم بجميع الأجهزة الكهربائية: إضاءة (Lighting)، تكييف "
            "وتدفئة (HVAC).\n"
            "- مستشعرات متنوعة: مستشعرات أبواب، مستشعرات جودة الهواء، "
            "مستشعرات حرارة ورطوبة.\n"
            "- أقفال أبواب ذكية (Door Locks)، إنتركوم (Intercom)، أنظمة "
            "كاميرات مراقبة (CCTV).\n"
            "- حلول شبكات كاملة: تمديد كابلات، خزانات (Cabinets)، تركيب "
            "WiFi، نقاط وصول (Access Points) وما إلى ذلك.\n"
            "\n"
            "### ب — البروتوكولات اللي نشتغل فيها\n"
            "- لاسلكية: Zigbee, Z-Wave, WiFi.\n"
            "- سلكية: DALI, Modbus, BACnet, KNX.\n"
            "\n"
            "## نقاط بنحب نضوّي عليها\n"
            "- في برايم ويف عنا حلول سلكية بأسعار تنافسية جداً ومش غالية. "
            "صحيح إنّو KNX و DALI والأنظمة السلكية بشكل عام معروفين إنّن "
            "غاليين، بس عنا حلول بأسعار معقولة وفي متناول الزبون. هاي "
            "نقطة قوة بميّزنا عن غيرنا."
        )
        # Schema describing the information Lena tries to collect during a
        # call. Each entry: {name, label, description}. The Gemini tool
        # `save_caller_information` is built dynamically from this list so
        # adding a new field needs no code change.
        self.slr_info_schema: list[dict[str, str]] = [
            {"name": "name",        "label": "الاسم",            "description": "اسم المتصل الثلاثي إن أمكن."},
            {"name": "address",     "label": "العنوان",          "description": "عنوان المتصل أو الموقع المطلوب تركيب النظام فيه."},
            {"name": "contact",     "label": "وسيلة التواصل",    "description": "رقم هاتف أو إيميل للتواصل لاحقاً."},
            {"name": "preference",  "label": "الاهتمامات",       "description": "ما يهتم به المتصل في البيت الذكي: تحكم إضاءة، HVAC، إنتركوم، إلخ."},
            {"name": "project_phase","label": "مرحلة المشروع",  "description": "مرحلة المشروع الحالية: تصميم، تنفيذ، تجديد، إلخ."},
        ]
        self._load()

    def _load(self) -> None:
        if not _STATE_PATH.exists():
            return
        try:
            data = json.loads(_STATE_PATH.read_text())
            self.frigate_url = data.get("frigate_url") or None
            self.homeassistant_url = data.get("homeassistant_url") or None
            self.homeassistant_token = data.get("homeassistant_token") or None
            self.gemini_api_key = data.get("gemini_api_key") or None
            self.gemini_model = data.get("gemini_model") or _DEFAULT_GEMINI_MODEL
            self.deepseek_api_key = data.get("deepseek_api_key") or None
            self.deepseek_base_url = data.get("deepseek_base_url") or "https://api.deepseek.com"
            self.openrouter_api_key = data.get("openrouter_api_key") or None
            self.openrouter_base_url = data.get("openrouter_base_url") or "https://openrouter.ai/api/v1"
            self.elevenlabs_api_key  = data.get("elevenlabs_api_key") or None
            self.elevenlabs_voice_id = data.get("elevenlabs_voice_id") or "EXAVITQu4vr4xnSDxMaL"
            self.elevenlabs_model_id = data.get("elevenlabs_model_id") or "eleven_multilingual_v2"
            vp = (data.get("voice_provider") or "gemini").strip().lower()
            self.voice_provider = vp if vp in (
                "gemini",
                "gemini_with_elevenlabs_tts",
                "elevenlabs_cai",
            ) else "gemini"
            self.elevenlabs_agent_id = data.get("elevenlabs_agent_id") or None
            self.mqtt_host = data.get("mqtt_host") or None
            self.mqtt_port = int(data.get("mqtt_port") or 1883)
            self.mqtt_username = data.get("mqtt_username") or None
            self.mqtt_password = data.get("mqtt_password") or None
            self.mqtt_topic_prefix = data.get("mqtt_topic_prefix") or "frigate"
            self.ollama_url = data.get("ollama_url") or None
            rules = data.get("ai_camera_rules")
            if isinstance(rules, list):
                self.ai_camera_rules = [r for r in rules if isinstance(r, dict)]
            self.pbx_host         = data.get("pbx_host") or None
            self.sip_ws_url       = data.get("sip_ws_url") or None
            self.sip_extension    = data.get("sip_extension") or None
            self.sip_password     = data.get("sip_password") or None
            self.sip_realm        = data.get("sip_realm") or None
            self.sip_display_name = data.get("sip_display_name") or None
            self.sla_enabled        = bool(data.get("sla_enabled"))
            self.sla_bind_host      = data.get("sla_bind_host") or "0.0.0.0"
            self.sla_bind_port      = int(data.get("sla_bind_port") or 8090)
            self.sla_system_prompt  = data.get("sla_system_prompt") or self.sla_system_prompt
            self.sla_voice          = data.get("sla_voice") or "Aoede"
            self.sla_greeting       = data.get("sla_greeting") or ""
            self.sla_enable_ha_tools = bool(data.get("sla_enable_ha_tools", True))
            self.sla_only_areas     = bool(data.get("sla_only_areas"))
            self.sla_max_call_s     = int(data.get("sla_max_call_s") or 600)
            cams = data.get("sla_cameras")
            if isinstance(cams, list):
                self.sla_cameras = [c for c in cams if isinstance(c, str)]
            self.slr_enabled       = bool(data.get("slr_enabled"))
            self.slr_bind_host     = data.get("slr_bind_host") or "0.0.0.0"
            self.slr_bind_port     = int(data.get("slr_bind_port") or 8091)
            self.slr_system_prompt = data.get("slr_system_prompt") or self.slr_system_prompt
            self.slr_voice         = data.get("slr_voice") or "Aoede"
            self.slr_greeting      = data.get("slr_greeting") if data.get("slr_greeting") is not None else self.slr_greeting
            self.slr_max_call_s    = int(data.get("slr_max_call_s") or 900)
            if "slr_interruption_enabled" in data:
                self.slr_interruption_enabled = bool(data.get("slr_interruption_enabled"))
            # Clinic Demo Live Agent (cda_*)
            self.cda_enabled       = bool(data.get("cda_enabled"))
            self.cda_bind_host     = data.get("cda_bind_host") or "0.0.0.0"
            self.cda_bind_port     = int(data.get("cda_bind_port") or 8092)
            self.cda_voice         = data.get("cda_voice") or "Aoede"
            self.cda_greeting      = data.get("cda_greeting") if data.get("cda_greeting") is not None else self.cda_greeting
            self.cda_max_call_s    = int(data.get("cda_max_call_s") or 900)
            if "cda_interruption_enabled" in data:
                self.cda_interruption_enabled = bool(data.get("cda_interruption_enabled"))
            kb = data.get("slr_knowledge")
            if isinstance(kb, str):
                self.slr_knowledge = kb
            schema = data.get("slr_info_schema")
            if isinstance(schema, list):
                self.slr_info_schema = [
                    s for s in schema
                    if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()
                ]
        except Exception:
            pass

    def save(self) -> None:
        with _lock:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(
                json.dumps(
                    {
                        "frigate_url": self.frigate_url,
                        "homeassistant_url": self.homeassistant_url,
                        "homeassistant_token": self.homeassistant_token,
                        "gemini_api_key": self.gemini_api_key,
                        "gemini_model": self.gemini_model,
                        "deepseek_api_key": self.deepseek_api_key,
                        "deepseek_base_url": self.deepseek_base_url,
                        "openrouter_api_key": self.openrouter_api_key,
                        "openrouter_base_url": self.openrouter_base_url,
                        "elevenlabs_api_key":  self.elevenlabs_api_key,
                        "elevenlabs_voice_id": self.elevenlabs_voice_id,
                        "elevenlabs_model_id": self.elevenlabs_model_id,
                        "voice_provider":      self.voice_provider,
                        "elevenlabs_agent_id": self.elevenlabs_agent_id,
                        "mqtt_host": self.mqtt_host,
                        "mqtt_port": self.mqtt_port,
                        "mqtt_username": self.mqtt_username,
                        "mqtt_password": self.mqtt_password,
                        "mqtt_topic_prefix": self.mqtt_topic_prefix,
                        "ollama_url": self.ollama_url,
                        "ai_camera_rules": self.ai_camera_rules,
                        "pbx_host":         self.pbx_host,
                        "sip_ws_url":       self.sip_ws_url,
                        "sip_extension":    self.sip_extension,
                        "sip_password":     self.sip_password,
                        "sip_realm":        self.sip_realm,
                        "sip_display_name": self.sip_display_name,
                        "sla_enabled":          self.sla_enabled,
                        "sla_bind_host":        self.sla_bind_host,
                        "sla_bind_port":        self.sla_bind_port,
                        "sla_system_prompt":    self.sla_system_prompt,
                        "sla_voice":            self.sla_voice,
                        "sla_greeting":         self.sla_greeting,
                        "sla_enable_ha_tools":  self.sla_enable_ha_tools,
                        "sla_only_areas":       self.sla_only_areas,
                        "sla_max_call_s":       self.sla_max_call_s,
                        "sla_cameras":          self.sla_cameras,
                        "slr_enabled":          self.slr_enabled,
                        "slr_bind_host":        self.slr_bind_host,
                        "slr_bind_port":        self.slr_bind_port,
                        "slr_system_prompt":    self.slr_system_prompt,
                        "slr_voice":            self.slr_voice,
                        "slr_greeting":         self.slr_greeting,
                        "slr_max_call_s":       self.slr_max_call_s,
                        "slr_interruption_enabled": self.slr_interruption_enabled,
                        "cda_enabled":          self.cda_enabled,
                        "cda_bind_host":        self.cda_bind_host,
                        "cda_bind_port":        self.cda_bind_port,
                        "cda_voice":            self.cda_voice,
                        "cda_greeting":         self.cda_greeting,
                        "cda_max_call_s":       self.cda_max_call_s,
                        "cda_interruption_enabled": self.cda_interruption_enabled,
                        "slr_knowledge":        self.slr_knowledge,
                        "slr_info_schema":      self.slr_info_schema,
                    },
                    indent=2,
                )
            )


state = State()
