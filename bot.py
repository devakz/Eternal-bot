"""
Bot de Discord com moderação + respostas automáticas simples + IA (Gemini opcional).

REQUISITOS:
    pip install discord.py
    pip install google-generativeai   # opcional, só se for usar o chat com IA

CONFIGURAÇÃO:
    1. Crie o bot em https://discord.com/developers/applications
    2. Ative em "Bot" -> "Privileged Gateway Intents": MESSAGE CONTENT INTENT
    3. Cole o token do bot na variável TOKEN abaixo (ou use variável de ambiente DISCORD_TOKEN)
    4. Convide o bot para o servidor com permissões de: Ban Members, Kick Members,
       Manage Channels, Manage Messages, Manage Roles, Send Messages, Read Message History
    5. (Opcional) Para o chat com IA: crie uma chave gratuita em https://aistudio.google.com/apikey
       e defina a variável de ambiente GEMINI_API_KEY. Sem isso, o bot usa respostas fixas normalmente.

COMO RODAR:
    python bot.py
"""

import os
import json
import random
import time
from datetime import timedelta, datetime, timezone
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "COLE_SEU_TOKEN_AQUI")

# --- Integração opcional com Gemini (chat inteligente) ---
# Deixe em branco / não configure a variável de ambiente para usar só as respostas fixas.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELO = "gemini-2.0-flash"
gemini_client = None

if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_client = genai.GenerativeModel(
            GEMINI_MODELO,
            system_instruction=(
                "Você é um bot de Discord simpático e prestativo. "
                "Responda de forma curta (no máximo 3-4 frases), casual e em português do Brasil."
            ),
        )
        print("✅ Integração com Gemini ativada.")
    except ImportError:
        print("⚠️  GEMINI_API_KEY definida, mas a biblioteca não está instalada. Rode: pip install google-generativeai")
        gemini_client = None

# Guarda um pequeno histórico de conversa por canal, pra dar contexto ao Gemini
historico_conversa = defaultdict(lambda: deque(maxlen=10))


async def perguntar_gemini(canal_id: int, autor: str, pergunta: str) -> str:
    if not gemini_client:
        return "A IA não está configurada. Peça pro administrador definir a variável GEMINI_API_KEY."

    historico = historico_conversa[canal_id]
    contexto = "\n".join(historico)
    prompt = f"{contexto}\n{autor}: {pergunta}" if contexto else f"{autor}: {pergunta}"

    try:
        resposta = await bot.loop.run_in_executor(None, lambda: gemini_client.generate_content(prompt))
        texto = resposta.text.strip()
    except Exception as e:
        return f"Deu um erro ao falar com a IA: {e}"

    historico.append(f"{autor}: {pergunta}")
    historico.append(f"Bot: {texto}")
    return texto

# Nome do canal onde o bot registra ações de moderação (crie esse canal no servidor)
CANAL_DE_LOGS = "mod-logs"

# Nome do cargo que pode ver e responder tickets (crie esse cargo no servidor)
CARGO_STAFF = "Staff"

# Nome da categoria onde os canais de ticket serão criados (crie essa categoria no servidor)
CATEGORIA_TICKETS = "Tickets"

# ---------------------------------------------------------------------------
# Configuração do bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# "Banco de dados" simples em arquivos JSON (sem precisar instalar nada)
# ---------------------------------------------------------------------------

ARQUIVO_WARNS = "warns.json"
ARQUIVO_XP = "xp.json"
ARQUIVO_PALAVROES = "palavras_proibidas.json"


def carregar_json(caminho, padrao):
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    return padrao


def salvar_json(caminho, dados):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


warns = carregar_json(ARQUIVO_WARNS, {})          # {guild_id: {user_id: [motivos]}}
xp_dados = carregar_json(ARQUIVO_XP, {})          # {guild_id: {user_id: {"xp": int, "nivel": int}}}
palavras_proibidas = set(carregar_json(ARQUIVO_PALAVROES, []))

# Controle de anti-spam: guarda timestamps das últimas mensagens de cada usuário
historico_mensagens = defaultdict(lambda: deque(maxlen=6))
SPAM_LIMITE_MSGS = 5      # mensagens
SPAM_JANELA_SEGUNDOS = 6  # nesse intervalo de tempo


async def enviar_log(guild: discord.Guild, texto: str):
    canal = discord.utils.get(guild.text_channels, name=CANAL_DE_LOGS)
    if canal:
        await canal.send(texto)


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    # Registra as views com botões (tickets) como persistentes, pra funcionarem mesmo após reiniciar o bot
    bot.add_view(AbrirTicketView())
    bot.add_view(FecharTicketView())
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} comandos de barra (/) sincronizados.")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    canal = discord.utils.get(member.guild.text_channels, name="geral")
    if canal:
        await canal.send(f"Bem-vindo(a) ao servidor, {member.mention}! 🎉")
    await enviar_log(member.guild, f"📥 {member.mention} entrou no servidor.")


@bot.event
async def on_member_remove(member: discord.Member):
    await enviar_log(member.guild, f"📤 {member} saiu do servidor.")


# ---------------------------------------------------------------------------
# Moderação
# ---------------------------------------------------------------------------

def tem_permissao_mod(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


@bot.tree.command(name="ban", description="Bane um usuário do servidor")
@app_commands.describe(membro="Usuário a ser banido", motivo="Motivo do banimento")
async def ban(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Não especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("Você não tem permissão para banir.", ephemeral=True)
    await membro.ban(reason=motivo)
    await interaction.response.send_message(f"🔨 {membro.mention} foi banido. Motivo: {motivo}")


@bot.tree.command(name="kick", description="Expulsa um usuário do servidor")
@app_commands.describe(membro="Usuário a ser expulso", motivo="Motivo da expulsão")
async def kick(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Não especificado"):
    if not interaction.user.guild_permissions.kick_members:
        return await interaction.response.send_message("Você não tem permissão para expulsar.", ephemeral=True)
    await membro.kick(reason=motivo)
    await interaction.response.send_message(f"👢 {membro.mention} foi expulso. Motivo: {motivo}")


@bot.tree.command(name="mute", description="Silencia um usuário (timeout)")
@app_commands.describe(membro="Usuário a silenciar", minutos="Duração em minutos")
async def mute(interaction: discord.Interaction, membro: discord.Member, minutos: int = 10):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("Você não tem permissão para silenciar.", ephemeral=True)
    from datetime import timedelta
    await membro.timeout(timedelta(minutes=minutos))
    await interaction.response.send_message(f"🔇 {membro.mention} foi silenciado por {minutos} minuto(s).")


@bot.tree.command(name="unmute", description="Remove o silenciamento de um usuário")
@app_commands.describe(membro="Usuário a dessilenciar")
async def unmute(interaction: discord.Interaction, membro: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await membro.timeout(None)
    await interaction.response.send_message(f"🔊 {membro.mention} pode falar novamente.")


@bot.tree.command(name="clear", description="Apaga mensagens do canal")
@app_commands.describe(quantidade="Quantas mensagens apagar (máx. 100)")
async def clear(interaction: discord.Interaction, quantidade: int = 10):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("Você não tem permissão para apagar mensagens.", ephemeral=True)
    quantidade = min(quantidade, 100)
    await interaction.response.defer(ephemeral=True)
    apagadas = await interaction.channel.purge(limit=quantidade)
    await interaction.followup.send(f"🧹 {len(apagadas)} mensagens apagadas.", ephemeral=True)


@bot.tree.command(name="nuke", description="Limpa o canal completamente (recria o canal do zero)")
async def nuke(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)

    canal = interaction.channel
    posicao = canal.position
    novo_canal = await canal.clone(reason=f"Nuke solicitado por {interaction.user}")
    await novo_canal.edit(position=posicao)
    await canal.delete()
    await novo_canal.send("💥 Canal reiniciado!")
    await enviar_log(interaction.guild, f"💥 {interaction.user} deu nuke no canal #{canal.name}.")


@bot.tree.command(name="unban", description="Remove o banimento de um usuário pelo ID")
@app_commands.describe(user_id="ID do usuário a desbanir")
async def unban(interaction: discord.Interaction, user_id: str):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    try:
        usuario = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(usuario)
        await interaction.response.send_message(f"✅ {usuario} foi desbanido.")
    except Exception:
        await interaction.response.send_message("Não encontrei esse ID banido.", ephemeral=True)


@bot.tree.command(name="lock", description="Bloqueia o canal atual (ninguém pode enviar mensagens)")
async def lock(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message("🔒 Canal bloqueado.")


@bot.tree.command(name="unlock", description="Desbloqueia o canal atual")
async def unlock(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await interaction.response.send_message("🔓 Canal desbloqueado.")


@bot.tree.command(name="slowmode", description="Define o modo lento do canal (em segundos)")
@app_commands.describe(segundos="Intervalo entre mensagens (0 desativa)")
async def slowmode(interaction: discord.Interaction, segundos: int):
    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await interaction.channel.edit(slowmode_delay=segundos)
    await interaction.response.send_message(f"🐌 Modo lento ajustado para {segundos}s.")


@bot.tree.command(name="softban", description="Bane e desbane na hora (apaga mensagens recentes sem banir de vez)")
@app_commands.describe(membro="Usuário", motivo="Motivo")
async def softban(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Não especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await membro.ban(reason=motivo, delete_message_days=1)
    await interaction.guild.unban(membro)
    await interaction.response.send_message(f"🧹 {membro.mention} levou softban (mensagens apagadas). Motivo: {motivo}")
    await enviar_log(interaction.guild, f"🧹 {membro} softban por {interaction.user}: {motivo}")


@bot.tree.command(name="tempban", description="Bane um usuário por um tempo determinado")
@app_commands.describe(membro="Usuário", horas="Duração em horas", motivo="Motivo")
async def tempban(interaction: discord.Interaction, membro: discord.Member, horas: int, motivo: str = "Não especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await membro.ban(reason=motivo)
    await interaction.response.send_message(f"⏳ {membro.mention} banido por {horas}h. Motivo: {motivo}")
    await enviar_log(interaction.guild, f"⏳ {membro} tempban ({horas}h) por {interaction.user}: {motivo}")

    async def desbanir_depois():
        import asyncio
        await asyncio.sleep(horas * 3600)
        try:
            await interaction.guild.unban(membro, reason="Tempban expirou")
            await enviar_log(interaction.guild, f"✅ Tempban de {membro} expirou, desbanido automaticamente.")
        except Exception:
            pass

    bot.loop.create_task(desbanir_depois())


@bot.tree.command(name="addrole", description="Adiciona um cargo a um usuário")
@app_commands.describe(membro="Usuário", cargo="Cargo a adicionar")
async def addrole(interaction: discord.Interaction, membro: discord.Member, cargo: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await membro.add_roles(cargo)
    await interaction.response.send_message(f"✅ Cargo {cargo.mention} adicionado a {membro.mention}.")


@bot.tree.command(name="removerole", description="Remove um cargo de um usuário")
@app_commands.describe(membro="Usuário", cargo="Cargo a remover")
async def removerole(interaction: discord.Interaction, membro: discord.Member, cargo: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await membro.remove_roles(cargo)
    await interaction.response.send_message(f"✅ Cargo {cargo.mention} removido de {membro.mention}.")


@bot.tree.command(name="purgeuser", description="Apaga as últimas mensagens de um usuário específico no canal")
@app_commands.describe(membro="Usuário", quantidade="Quantas mensagens verificar (máx. 200)")
async def purgeuser(interaction: discord.Interaction, membro: discord.Member, quantidade: int = 50):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    quantidade = min(quantidade, 200)
    await interaction.response.defer(ephemeral=True)
    apagadas = await interaction.channel.purge(limit=quantidade, check=lambda m: m.author.id == membro.id)
    await interaction.followup.send(f"🧹 {len(apagadas)} mensagens de {membro.mention} apagadas.", ephemeral=True)


@bot.tree.command(name="purgecontains", description="Apaga mensagens recentes que contenham um texto")
@app_commands.describe(texto="Texto a buscar", quantidade="Quantas mensagens verificar (máx. 200)")
async def purgecontains(interaction: discord.Interaction, texto: str, quantidade: int = 50):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    quantidade = min(quantidade, 200)
    await interaction.response.defer(ephemeral=True)
    apagadas = await interaction.channel.purge(limit=quantidade, check=lambda m: texto.lower() in m.content.lower())
    await interaction.followup.send(f"🧹 {len(apagadas)} mensagens contendo '{texto}' apagadas.", ephemeral=True)


# ---------------------------------------------------------------------------
# Sistema de avisos (warns)
# ---------------------------------------------------------------------------

@bot.tree.command(name="warn", description="Aplica um aviso a um usuário")
@app_commands.describe(membro="Usuário a avisar", motivo="Motivo do aviso")
async def warn(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Não especificado"):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)

    gid, uid = str(interaction.guild.id), str(membro.id)
    warns.setdefault(gid, {}).setdefault(uid, []).append(motivo)
    salvar_json(ARQUIVO_WARNS, warns)

    total = len(warns[gid][uid])
    await interaction.response.send_message(f"⚠️ {membro.mention} recebeu um aviso ({total} no total). Motivo: {motivo}")
    await enviar_log(interaction.guild, f"⚠️ {membro} avisado por {interaction.user}: {motivo}")

    if total >= 3:
        await membro.timeout(timedelta(minutes=30), reason="3 avisos acumulados")
        await interaction.followup.send(f"🔇 {membro.mention} atingiu 3 avisos e foi silenciado automaticamente por 30 min.")


@bot.tree.command(name="warns", description="Lista os avisos de um usuário")
@app_commands.describe(membro="Usuário a consultar")
async def listar_warns(interaction: discord.Interaction, membro: discord.Member):
    gid, uid = str(interaction.guild.id), str(membro.id)
    lista = warns.get(gid, {}).get(uid, [])
    if not lista:
        return await interaction.response.send_message(f"{membro.mention} não tem avisos.", ephemeral=True)
    texto = "\n".join(f"{i+1}. {m}" for i, m in enumerate(lista))
    await interaction.response.send_message(f"Avisos de {membro.mention}:\n{texto}", ephemeral=True)


@bot.tree.command(name="clearwarns", description="Remove todos os avisos de um usuário")
@app_commands.describe(membro="Usuário a limpar")
async def clearwarns(interaction: discord.Interaction, membro: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    gid, uid = str(interaction.guild.id), str(membro.id)
    warns.get(gid, {}).pop(uid, None)
    salvar_json(ARQUIVO_WARNS, warns)
    await interaction.response.send_message(f"✅ Avisos de {membro.mention} foram limpos.")


# ---------------------------------------------------------------------------
# Filtro de palavras proibidas (automod)
# ---------------------------------------------------------------------------

@bot.tree.command(name="addpalavra", description="Adiciona uma palavra à lista de banidas (auto-exclusão)")
@app_commands.describe(palavra="Palavra a proibir")
async def addpalavra(interaction: discord.Interaction, palavra: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    palavras_proibidas.add(palavra.lower())
    salvar_json(ARQUIVO_PALAVROES, list(palavras_proibidas))
    await interaction.response.send_message(f"🚫 Palavra adicionada ao filtro.", ephemeral=True)


@bot.tree.command(name="removepalavra", description="Remove uma palavra da lista de banidas")
@app_commands.describe(palavra="Palavra a remover")
async def removepalavra(interaction: discord.Interaction, palavra: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    palavras_proibidas.discard(palavra.lower())
    salvar_json(ARQUIVO_PALAVROES, list(palavras_proibidas))
    await interaction.response.send_message(f"✅ Palavra removida do filtro.", ephemeral=True)


# ---------------------------------------------------------------------------
# Sistema de XP / nível
# ---------------------------------------------------------------------------

def xp_para_proximo_nivel(nivel: int) -> int:
    return 100 + (nivel * 50)


def adicionar_xp(guild_id: str, user_id: str) -> tuple[bool, int]:
    """Retorna (subiu_de_nivel, novo_nivel)."""
    dados_guild = xp_dados.setdefault(guild_id, {})
    perfil = dados_guild.setdefault(user_id, {"xp": 0, "nivel": 1})
    perfil["xp"] += random.randint(5, 15)

    subiu = False
    while perfil["xp"] >= xp_para_proximo_nivel(perfil["nivel"]):
        perfil["xp"] -= xp_para_proximo_nivel(perfil["nivel"])
        perfil["nivel"] += 1
        subiu = True

    salvar_json(ARQUIVO_XP, xp_dados)
    return subiu, perfil["nivel"]


@bot.tree.command(name="nivel", description="Mostra seu nível e XP")
@app_commands.describe(membro="Usuário a consultar (opcional)")
async def nivel(interaction: discord.Interaction, membro: discord.Member = None):
    membro = membro or interaction.user
    perfil = xp_dados.get(str(interaction.guild.id), {}).get(str(membro.id), {"xp": 0, "nivel": 1})
    faltam = xp_para_proximo_nivel(perfil["nivel"]) - perfil["xp"]
    await interaction.response.send_message(
        f"📊 {membro.mention} está no nível **{perfil['nivel']}** ({perfil['xp']} XP, faltam {faltam} para o próximo nível)."
    )


@bot.tree.command(name="ranking", description="Mostra o ranking de níveis do servidor")
async def ranking(interaction: discord.Interaction):
    dados_guild = xp_dados.get(str(interaction.guild.id), {})
    if not dados_guild:
        return await interaction.response.send_message("Ainda não há dados de XP neste servidor.", ephemeral=True)
    top = sorted(dados_guild.items(), key=lambda kv: (kv[1]["nivel"], kv[1]["xp"]), reverse=True)[:10]
    linhas = []
    for i, (uid, perfil) in enumerate(top, start=1):
        membro = interaction.guild.get_member(int(uid))
        nome = membro.display_name if membro else f"Usuário {uid}"
        linhas.append(f"{i}. {nome} — nível {perfil['nivel']} ({perfil['xp']} XP)")
    await interaction.response.send_message("🏆 **Ranking do servidor**\n" + "\n".join(linhas))


# ---------------------------------------------------------------------------
# Informações
# ---------------------------------------------------------------------------

@bot.tree.command(name="userinfo", description="Mostra informações de um usuário")
@app_commands.describe(membro="Usuário a consultar (opcional)")
async def userinfo(interaction: discord.Interaction, membro: discord.Member = None):
    membro = membro or interaction.user
    embed = discord.Embed(title=f"Informações de {membro.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=membro.display_avatar.url)
    embed.add_field(name="ID", value=membro.id, inline=True)
    embed.add_field(name="Entrou em", value=membro.joined_at.strftime("%d/%m/%Y") if membro.joined_at else "—", inline=True)
    embed.add_field(name="Conta criada em", value=membro.created_at.strftime("%d/%m/%Y"), inline=True)
    cargos = ", ".join(r.mention for r in membro.roles if r.name != "@everyone") or "Nenhum"
    embed.add_field(name="Cargos", value=cargos, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Mostra informações do servidor")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=guild.name, color=discord.Color.green())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Membros", value=guild.member_count, inline=True)
    embed.add_field(name="Canais de texto", value=len(guild.text_channels), inline=True)
    embed.add_field(name="Canais de voz", value=len(guild.voice_channels), inline=True)
    embed.add_field(name="Cargos", value=len(guild.roles), inline=True)
    embed.add_field(name="Criado em", value=guild.created_at.strftime("%d/%m/%Y"), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="chat", description="Conversa com a IA do bot (Gemini)")
@app_commands.describe(mensagem="O que você quer perguntar ou falar")
async def chat_cmd(interaction: discord.Interaction, mensagem: str):
    if not gemini_client:
        return await interaction.response.send_message(
            "A IA não está configurada. Defina a variável de ambiente GEMINI_API_KEY.", ephemeral=True
        )
    await interaction.response.defer()
    resposta = await perguntar_gemini(interaction.channel.id, interaction.user.display_name, mensagem)
    await interaction.followup.send(resposta[:2000])


@bot.tree.command(name="help", description="Mostra todos os comandos disponíveis")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Comandos do bot", color=discord.Color.orange())
    embed.add_field(
        name="🔨 Moderação",
        value="/ban /kick /softban /tempban /mute /unmute /unban /clear /nuke /lock /unlock /slowmode /purgeuser /purgecontains /addrole /removerole",
        inline=False,
    )
    embed.add_field(name="⚠️ Avisos", value="/warn /warns /clearwarns", inline=False)
    embed.add_field(name="🚫 Automod", value="/addpalavra /removepalavra (+ anti-spam automático)", inline=False)
    embed.add_field(name="📊 Níveis", value="/nivel /ranking", inline=False)
    embed.add_field(name="ℹ️ Informações", value="/userinfo /serverinfo", inline=False)
    embed.add_field(name="🎭 Cargo por reação", value="/setup-reacao", inline=False)
    embed.add_field(name="🎨 Embeds", value="/embed (formulário para criar embed customizado)", inline=False)
    embed.add_field(
        name="🤖 Chat com IA",
        value="/chat (ou mencione o bot) — respostas inteligentes via Gemini" if gemini_client else "/chat (não configurado — defina GEMINI_API_KEY)",
        inline=False,
    )
    embed.add_field(
        name="🎫 Tickets",
        value="/painel-tickets (posta o botão) /fechar-ticket",
        inline=False,
    )
    embed.set_footer(text="Alguns comandos exigem permissões de moderação/admin.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Comando /embed — cria uma mensagem embed customizada via formulário
# ---------------------------------------------------------------------------

class EmbedModal(discord.ui.Modal, title="Criar Embed"):
    titulo_campo = discord.ui.TextInput(label="Título", max_length=256, required=True)
    descricao_campo = discord.ui.TextInput(
        label="Descrição", style=discord.TextStyle.paragraph, max_length=4000, required=True
    )
    cor_campo = discord.ui.TextInput(
        label="Cor (hex, ex: #5865F2)", required=False, max_length=7, placeholder="#5865F2"
    )
    imagem_campo = discord.ui.TextInput(label="URL da imagem (opcional)", required=False)
    rodape_campo = discord.ui.TextInput(label="Rodapé (opcional)", required=False, max_length=256)

    async def on_submit(self, interaction: discord.Interaction):
        cor = discord.Color.blurple()
        if self.cor_campo.value:
            try:
                cor = discord.Color(int(self.cor_campo.value.strip().lstrip("#"), 16))
            except ValueError:
                pass

        embed = discord.Embed(title=self.titulo_campo.value, description=self.descricao_campo.value, color=cor)
        if self.imagem_campo.value:
            embed.set_image(url=self.imagem_campo.value)
        if self.rodape_campo.value:
            embed.set_footer(text=self.rodape_campo.value)

        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="embed", description="Cria uma mensagem embed personalizada")
async def embed_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    await interaction.response.send_modal(EmbedModal())


# ---------------------------------------------------------------------------
# Sistema de tickets
# ---------------------------------------------------------------------------

class FecharTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar Ticket", style=discord.ButtonStyle.danger, custom_id="fechar_ticket_botao")
    async def fechar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Este ticket será fechado em 5 segundos...")
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete()


class AbrirTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Abrir Ticket", style=discord.ButtonStyle.primary, custom_id="abrir_ticket_botao")
    async def abrir(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        autor = interaction.user

        nome_canal = f"ticket-{autor.name}".lower().replace(" ", "-")
        existente = discord.utils.get(guild.text_channels, name=nome_canal)
        if existente:
            return await interaction.response.send_message(
                f"Você já tem um ticket aberto: {existente.mention}", ephemeral=True
            )

        categoria = discord.utils.get(guild.categories, name=CATEGORIA_TICKETS)
        cargo_staff = discord.utils.get(guild.roles, name=CARGO_STAFF)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            autor: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if cargo_staff:
            overwrites[cargo_staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        canal_ticket = await guild.create_text_channel(
            nome_canal, category=categoria, overwrites=overwrites, reason=f"Ticket aberto por {autor}"
        )

        embed = discord.Embed(
            title="🎫 Ticket aberto",
            description=f"Olá {autor.mention}! Descreva seu problema e aguarde, a equipe vai te atender em breve.",
            color=discord.Color.blue(),
        )
        await canal_ticket.send(embed=embed, view=FecharTicketView())
        await interaction.response.send_message(f"✅ Ticket criado: {canal_ticket.mention}", ephemeral=True)
        await enviar_log(guild, f"🎫 {autor} abriu um ticket: {canal_ticket.mention}")


@bot.tree.command(name="painel-tickets", description="Envia o painel com o botão de abrir ticket neste canal")
async def painel_tickets(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    embed = discord.Embed(
        title="📩 Central de Suporte",
        description="Precisa de ajuda? Clique no botão abaixo para abrir um ticket privado com a equipe.",
        color=discord.Color.blue(),
    )
    await interaction.channel.send(embed=embed, view=AbrirTicketView())
    await interaction.response.send_message("✅ Painel de tickets enviado.", ephemeral=True)


@bot.tree.command(name="fechar-ticket", description="Fecha o ticket atual (use dentro do canal do ticket)")
async def fechar_ticket(interaction: discord.Interaction):
    if not interaction.channel.name.startswith("ticket-"):
        return await interaction.response.send_message("Este comando só funciona dentro de um canal de ticket.", ephemeral=True)
    await interaction.response.send_message("🔒 Fechando ticket em 5 segundos...")
    import asyncio
    await asyncio.sleep(5)
    await interaction.channel.delete()


# ---------------------------------------------------------------------------
# Cargo por reação
# ---------------------------------------------------------------------------

reacoes_para_cargo = {}  # {message_id: {emoji_str: role_id}}


@bot.tree.command(name="setup-reacao", description="Configura um cargo obtido por reação em uma mensagem")
@app_commands.describe(mensagem_id="ID da mensagem", emoji="Emoji a usar", cargo="Cargo a dar")
async def setup_reacao(interaction: discord.Interaction, mensagem_id: str, emoji: str, cargo: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("Você não tem permissão para isso.", ephemeral=True)
    try:
        mensagem = await interaction.channel.fetch_message(int(mensagem_id))
    except Exception:
        return await interaction.response.send_message("Mensagem não encontrada neste canal.", ephemeral=True)

    await mensagem.add_reaction(emoji)
    reacoes_para_cargo.setdefault(mensagem.id, {})[emoji] = cargo.id
    await interaction.response.send_message(f"✅ Reagir com {emoji} nessa mensagem dará o cargo {cargo.mention}.", ephemeral=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.message_id not in reacoes_para_cargo:
        return
    emoji = str(payload.emoji)
    cargo_id = reacoes_para_cargo[payload.message_id].get(emoji)
    if not cargo_id:
        return
    guild = bot.get_guild(payload.guild_id)
    cargo = guild.get_role(cargo_id)
    membro = guild.get_member(payload.user_id)
    if cargo and membro and not membro.bot:
        await membro.add_roles(cargo)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.message_id not in reacoes_para_cargo:
        return
    emoji = str(payload.emoji)
    cargo_id = reacoes_para_cargo[payload.message_id].get(emoji)
    if not cargo_id:
        return
    guild = bot.get_guild(payload.guild_id)
    cargo = guild.get_role(cargo_id)
    membro = guild.get_member(payload.user_id)
    if cargo and membro:
        await membro.remove_roles(cargo)


# ---------------------------------------------------------------------------
# Chat / respostas automáticas simples (sem IA, sem custo)
# ---------------------------------------------------------------------------

RESPOSTAS = {
    "oi": ["Oi! Tudo bem? 👋", "E aí!", "Opa, chegou!"],
    "bom dia": ["Bom dia! ☀️", "Bom dia pra você também!"],
    "boa noite": ["Boa noite! 🌙", "Descansa bem!"],
    "obrigado": ["De nada! 😊", "Disponha!"],
    "ajuda": ["Use `/help` para ver todos os comandos disponíveis."],
    "tudo bem": ["Tudo ótimo por aqui, e você?", "Tudo certo! 🙂"],
}


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    conteudo = message.content.lower()

    # --- Filtro de palavras proibidas ---
    if any(palavra in conteudo for palavra in palavras_proibidas):
        try:
            await message.delete()
            aviso = await message.channel.send(f"{message.author.mention}, essa palavra não é permitida aqui.")
            await enviar_log(message.guild, f"🚫 Mensagem de {message.author} removida (palavra proibida).")
        except discord.Forbidden:
            pass
        return

    # --- Anti-spam: X mensagens em Y segundos leva a timeout curto ---
    agora = time.time()
    chave_usuario = (message.guild.id, message.author.id)
    historico = historico_mensagens[chave_usuario]
    historico.append(agora)
    if len(historico) == historico.maxlen and (agora - historico[0]) < SPAM_JANELA_SEGUNDOS:
        if isinstance(message.author, discord.Member) and message.author.guild_permissions.moderate_members is False:
            try:
                await message.author.timeout(timedelta(minutes=5), reason="Spam detectado")
                await message.channel.send(f"🚫 {message.author.mention} foi silenciado por 5 min (spam).")
                await enviar_log(message.guild, f"🚫 {message.author} silenciado automaticamente por spam.")
                historico.clear()
            except discord.Forbidden:
                pass

    # --- Chat com IA (Gemini) quando o bot é mencionado ---
    if gemini_client and bot.user in message.mentions:
        pergunta = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if pergunta:
            async with message.channel.typing():
                resposta = await perguntar_gemini(message.channel.id, message.author.display_name, pergunta)
            await message.reply(resposta[:2000])
            await bot.process_commands(message)
            return

    # --- Respostas automáticas por palavra-chave ---
    for chave, respostas in RESPOSTAS.items():
        if chave in conteudo:
            await message.channel.send(random.choice(respostas))
            break

    # --- Sistema de XP (ganha XP a cada mensagem, com cooldown implícito pelo histórico) ---
    subiu, novo_nivel = adicionar_xp(str(message.guild.id), str(message.author.id))
    if subiu:
        await message.channel.send(f"🎉 {message.author.mention} subiu para o nível **{novo_nivel}**!")

    await bot.process_commands(message)


if __name__ == "__main__":
    if TOKEN == "COLE_SEU_TOKEN_AQUI":
        print("⚠️  Configure seu token do Discord antes de rodar (variável TOKEN ou DISCORD_TOKEN).")
    bot.run(TOKEN)
