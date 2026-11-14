#!/usr/bin/env python3
"""
Monitor de concursos FGV.
Lê a seção "Arquivos do concurso" de cada página, compara com a última
execução (state.json) e envia aviso no Telegram quando surge item novo.
"""
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SITES = {
    "Educação Básica": "https://conhecimento.fgv.br/concursos/seducsp26edbasica",
    "Educação Técnica": "https://conhecimento.fgv.br/concursos/seducsp26tecnico",
}

STATE_FILE = Path(__file__).parent / "state.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; monitor-concurso/1.0)"}
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
MAX_ITENS_POR_SITE = 12  # evita estourar o limite de 4096 caracteres do Telegram


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
                break  # fim da seção
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


def enviar_telegram(texto: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("[aviso] Telegram não configurado. Mensagem:\n" + texto)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": texto[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def main() -> int:
    estado = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    primeira_execucao = not estado
    novo_estado = dict(estado)
    mensagens, erros = [], []

    for nome, url in SITES.items():
        try:
            itens = extrair_itens(baixar(url))
        except Exception as e:
            erros.append(f"{nome}: falha ao acessar ({e})")
            continue

        if not itens:
            # Não sobrescreve o estado: provavelmente o layout mudou.
            erros.append(f"{nome}: nenhum item encontrado (o layout do site pode ter mudado)")
            continue

        conhecidos = set(estado.get(nome, []))
        novos = [i for i in itens if chave(i) not in conhecidos]
        novo_estado[nome] = [chave(i) for i in itens]

        if novos and not primeira_execucao:
            linhas = [f"🔔 Novidade - {nome}"]
            for i in novos[:MAX_ITENS_POR_SITE]:
                linhas.append(f"\n{i['data']} - {i['titulo']}\n{i['link']}")
            if len(novos) > MAX_ITENS_POR_SITE:
                linhas.append(f"\n... e mais {len(novos) - MAX_ITENS_POR_SITE} itens.")
            linhas.append(f"\nPágina: {url}")
            mensagens.append("\n".join(linhas))

    if primeira_execucao and novo_estado:
        enviar_telegram(
            "✅ Monitoramento iniciado. Você será avisado quando houver "
            "atualização nas páginas da FGV."
        )

    for m in mensagens:
        enviar_telegram(m)

    if erros:
        print("\n".join(erros), file=sys.stderr)

    STATE_FILE.write_text(json.dumps(novo_estado, ensure_ascii=False, indent=2))
    return 1 if erros and not mensagens else 0


if __name__ == "__main__":
    sys.exit(main())
