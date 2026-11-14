#!/usr/bin/env python3
"""
Monitor de concursos FGV com controle de frequência via Telegram.

Cada execução (a cada 5 min pelo GitHub Actions):
  1. Lê comandos/botões enviados ao bot (/frequencia, /status, /ajuda).
  2. Se já estiver na hora (conforme a frequência escolhida), verifica os sites.
  3. A cada verificação, envia uma mensagem: com novidades ou "sem novidades".
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SITES = {
    "Educação Básica": "https://conhecimento.fgv.br/concursos/seducsp26edbasica",
    "Educação Técnica": "https://conhecimento.fgv.br/concursos/seducsp26tecnico",
}

STATE_FILE = Path(__file__).parent / "state.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

TZ = ZoneInfo("America/Sao_Paulo")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; monitor-concurso/1.0)"}
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
MAX_ITENS_POR_SITE = 12

# Opções de frequência. Para adicionar uma nova, basta incluir aqui.
OPCOES = {
    "30min": {"rotulo": "A cada 30 minutos", "intervalo": 30},
    "2h": {"rotulo": "A cada 2 horas", "intervalo": 120},
    "3x": {"rotulo": "3 vezes ao dia (8h, 13h e 18h)", "horarios": [8, 13, 18]},
}
PADRAO = "30min"
TOLERANCIA = timedelta(minutes=3)  # absorve atrasos do agendador do GitHub

AJUDA = (
    "Comandos:\n"
    "/frequencia - escolher a frequência das verificações\n"
    "/status - ver a configuração atual\n"
    "/ajuda - mostrar esta mensagem\n\n"
    "Obs.: o bot confere seus comandos a cada ~5 minutos, então a resposta "
    "pode demorar um pouco."
)


# ---------------------------------------------------------------- Telegram
def tg(metodo: str, **payload):
    r = requests.post(f"{API}/{metodo}", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def enviar(texto: str, teclado: dict | None = None) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("[aviso] Telegram não configurado. Mensagem:\n" + texto)
        return
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto[:4000],
        "disable_web_page_preview": True,
    }
    if teclado:
        payload["reply_markup"] = teclado
    tg("sendMessage", **payload)


def teclado_frequencia(atual: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": ("✓ " if k == atual else "") + v["rotulo"], "callback_data": f"freq:{k}"}]
            for k, v in OPCOES.items()
        ]
    }


def texto_status(estado: dict) -> str:
    freq = estado["config"]["frequencia"]
    ultima = estado.get("last_check")
    if ultima:
        ultima = datetime.fromisoformat(ultima).astimezone(TZ).strftime("%d/%m/%Y %H:%M")
    else:
        ultima = "ainda não verificado"
    return f"Frequência: {OPCOES[freq]['rotulo']}\nÚltima verificação: {ultima}"


def processar_comandos(estado: dict) -> bool:
    """Lê mensagens novas do Telegram. Retorna True se a frequência mudou."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False

    params = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
    if estado["telegram_offset"]:
        params["offset"] = estado["telegram_offset"] + 1
    updates = tg("getUpdates", **params).get("result", [])

    mudou = False
    for u in updates:
        estado["telegram_offset"] = u["update_id"]

        if "message" in u:
            msg = u["message"]
            if str(msg.get("chat", {}).get("id")) != TELEGRAM_CHAT_ID:
                continue  # ignora qualquer pessoa que não seja você
            partes = (msg.get("text") or "").strip().split()
            if not partes:
                continue
            cmd = partes[0].split("@")[0].lower()
            if cmd == "/frequencia":
                enviar(
                    "Com que frequência quer que eu verifique os sites?",
                    teclado_frequencia(estado["config"]["frequencia"]),
                )
            elif cmd == "/status":
                enviar(texto_status(estado))
            elif cmd in ("/start", "/ajuda", "/help"):
                enviar(AJUDA)

        elif "callback_query" in u:
            cq = u["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != TELEGRAM_CHAT_ID:
                continue
            try:
                tg("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass  # o botão pode ter expirado; não é problema
            data = cq.get("data", "")
            if data.startswith("freq:"):
                escolha = data.split(":", 1)[1]
                if escolha in OPCOES:
                    estado["config"]["frequencia"] = escolha
                    mudou = True
                    enviar(
                        f"✅ Frequência alterada: {OPCOES[escolha]['rotulo']}.\n"
                        "Vou fazer uma verificação agora."
                    )
    return mudou


# ---------------------------------------------------------------- Raspagem
def baixar(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def extrair_itens(html: str) -> list[dict]:
    """Retorna os itens da seção 'Arquivos do concurso' (data, título, link)."""
    soup = BeautifulSoup(html, "html.parser")

    inicio = None
    for h in soup.find_all(["h2", "h3", "h4"]):
        if "arquivos do concurso" in h.get_text(strip=True).lower():
            inicio = h
            break
    if inicio is None:
        return []

    itens, data_atual = [], ""
    for el in inicio.next_elements:
        if isinstance(el, Tag):
            if el.name in ("h2", "h3") and "receba" in el.get_text().lower():
                break
            if el.name == "a" and el.get("href"):
                titulo = el.get_text(" ", strip=True)
                if titulo:
                    itens.append({"data": data_atual, "titulo": titulo, "link": el["href"]})
        elif isinstance(el, NavigableString):
            txt = el.strip()
            if DATE_RE.match(txt):
                data_atual = txt
    return itens


def chave(item: dict) -> str:
    return f'{item["data"]}|{item["titulo"]}|{item["link"]}'


# ---------------------------------------------------------------- Agenda
def deve_verificar(estado: dict, agora: datetime) -> bool:
    ultima = estado.get("last_check")
    if not ultima:
        return True
    ultima = datetime.fromisoformat(ultima)
    cfg = OPCOES[estado["config"]["frequencia"]]

    if "intervalo" in cfg:
        return agora - ultima >= timedelta(minutes=cfg["intervalo"]) - TOLERANCIA

    # Horários fixos: já passou algum horário desde a última verificação?
    local = agora.astimezone(TZ)
    passados = []
    for dias_atras in (0, 1):
        d = (local - timedelta(days=dias_atras)).date()
        for h in cfg["horarios"]:
            slot = datetime(d.year, d.month, d.day, h, tzinfo=TZ)
            if slot <= local:
                passados.append(slot)
    return ultima < max(passados)


# ---------------------------------------------------------------- Verificação
def executar_verificacao(estado: dict, agora: datetime, primeira: bool) -> None:
    novidades, resumo, erros = [], [], []

    for nome, url in SITES.items():
        try:
            itens = extrair_itens(baixar(url))
        except Exception as e:
            erros.append(f"{nome}: falha ao acessar ({e})")
            continue
        if not itens:
            erros.append(f"{nome}: nenhum item encontrado (o layout do site pode ter mudado)")
            continue

        conhecidos = set(estado["sites"].get(nome, []))
        novos = [i for i in itens if chave(i) not in conhecidos]
        estado["sites"][nome] = [chave(i) for i in itens]
        resumo.append(f"• {nome}: {len(itens)} itens listados")

        if novos and not primeira:
            linhas = [f"🔔 Novidade - {nome}"]
            for i in novos[:MAX_ITENS_POR_SITE]:
                linhas.append(f"\n{i['data']} - {i['titulo']}\n{i['link']}")
            if len(novos) > MAX_ITENS_POR_SITE:
                linhas.append(f"\n... e mais {len(novos) - MAX_ITENS_POR_SITE} itens.")
            linhas.append(f"\nPágina: {url}")
            novidades.append("\n".join(linhas))

    estado["last_check"] = agora.isoformat()
    rotulo = OPCOES[estado["config"]["frequencia"]]["rotulo"]
    hora = agora.astimezone(TZ).strftime("%H:%M")

    if primeira and estado["sites"]:
        enviar(
            "✅ Monitoramento iniciado.\n"
            + "\n".join(resumo)
            + f"\n\nFrequência atual: {rotulo}.\nUse /frequencia para mudar ou /ajuda."
        )
    for m in novidades:
        enviar(m)
    if erros:
        enviar("⚠️ Problema na verificação das " + hora + ":\n" + "\n".join(erros))
    if not primeira and not novidades and not erros:
        enviar(f"✅ Verificação das {hora}: sem novidades.\n" + "\n".join(resumo)
               + f"\nFrequência: {rotulo}")


# ---------------------------------------------------------------- Estado
def carregar_estado() -> dict:
    bruto = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    # Compatível com o formato antigo, que guardava só {site: [chaves]}
    estado = bruto if "sites" in bruto else {"sites": bruto}
    estado.setdefault("config", {})
    if estado["config"].get("frequencia") not in OPCOES:
        estado["config"]["frequencia"] = PADRAO
    estado.setdefault("last_check", None)
    estado.setdefault("telegram_offset", 0)
    return estado


def main() -> int:
    estado = carregar_estado()
    agora = datetime.now(timezone.utc)
    primeira = not estado["sites"]

    try:
        if processar_comandos(estado):
            estado["last_check"] = None  # força verificação imediata
        if deve_verificar(estado, agora):
            executar_verificacao(estado, agora, primeira)
    finally:
        STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())