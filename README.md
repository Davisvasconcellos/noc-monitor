# 🌐 Monitor NOC - Videoconferência & Infraestrutura

> **Sistema profissional e autônomo para monitoramento em tempo real de salas de reunião, equipamentos de videoconferência e infraestrutura de rede local.**

Desenvolvido para ambientes corporativos com suporte a **Camada 2 (Win32 SendARP)**, tolerância total a trocas de IP via DHCP (**Auto-Healing**), notificações de alarme por e-mail e integração nativa com a bandeja do sistema Windows (System Tray).

---

## 🚀 Principais Funcionalidades

### 🔄 Rastreamento DHCP Inteligente & Auto-Healing (Camada 2)
* **Chamada Direta ao Kernel do Windows (`SendARP`)**:
  - Consulta o endereço MAC físico de qualquer equipamento na sub-rede local em menos de **1ms**, contornando bloqueios de firewall/ICMP.
* **Modo Dinâmico (DHCP Auto-Tracking)**:
  - Se um dispositivo perder conexão e o roteador/DHCP atribuir um **novo endereço IP**, o monitor localiza o equipamento automaticamente pelo MAC físico e Hostname, atualizando a base sem declarar falso alerta de offline.
* **Modo Fixo (Estático)**:
  - Para servidores, switches ou gateways que possuem IP fixo configurado manualmente.
* **Mecanismo de Verificação em 3 Camadas no Ping Sweep**:
  1. *IP Atual*: Dispara ping ICMP no endereço registrado.
  2. *Hostname/DNS*: Se offline e em modo dinâmico, resolve o Hostname DNS registrado.
  3. *MAC Address / SendARP*: Varre o cache ARP e a sub-rede em busca do MAC físico do equipamento.
* **DHCP Watchdog em Segundo Plano**:
  - Loop automático a cada 60 segundos reconciliando alterações de IP em segundo plano.

---

### 🔍 Auto-Identificação de Equipamentos ("Checar IP")
* No formulário de cadastro individual:
  - Ao digitar o IP e clicar em **🔍 Checar IP**, o sistema realiza sondagem instantânea via ICMP, portas TCP (443, 80, 5060, 8080) e resolução reversa de DNS.
  - Preenche automaticamente o **MAC Address**, **Hostname** e **Fabricante/Modelo** (*Poly / HP, Cisco, Yealink, Logitech, Crestron, Lifesize*).

---

### 📡 Varredura de Rede Local (Network IP Range Scanner)
* Descoberta automática de até 254 endereços IP simultâneos com multithreading em 3 a 5 segundos.
* Cadastro em lote selecionando salas e tipos de monitoramento (IP ou Hostname).

---

### ✉️ Notificações de Alarme & Modo de Teste de E-mail
* **Alertas Automáticos**:
  - Disparo imediato de e-mail formatado em HTML quando uma sala/equipamento ficar **Offline** ou quando for **Recuperado (Online)**.
* **Card Dedicado de Teste de E-mail**:
  - Teste imediato de credenciais e conectividade com diagnóstico em tempo real sem precisar salvar previamente.
* **Presets de Provedor em 1 Clique**:
  - **Microsoft 365** (`smtp.office365.com:587` com STARTTLS).
  - **Gmail** (`smtp.gmail.com:587` com suporte automático a Senhas de Aplicativo).
  - **Outlook.com** (`smtp-mail.outlook.com:587`).
* **Tooltips de Ajuda Integrados**:
  - Instruções detalhadas para criação de Senhas de App no Google e resolução de códigos do Exchange Online.

---

### 🖥️ Interface Moderna NOC & System Tray
* Painel web com design dark mode profissional e responsivo.
* Agrupamento visual por Salas e visão detalhada de Equipamentos.
* Gráfico de histórico recente (Linha do Tempo) e métrica de **Disponibilidade 24h (Uptime %)**.
* Alarme sonoro opcional via Web Audio API.
* Execução em segundo plano com ícone na bandeja do sistema Windows junto ao relógio.

---

## 🛠️ Tecnologias Utilizadas

* **Linguagem**: Python 3.14+ (64-bit)
* **Backend Web**: Flask
* **Banco de Dados**: SQLite (com auto-limpeza de logs com mais de 7 dias)
* **Conectividade de Baixo Nível**: `ctypes` (Win32 `iphlpapi.dll` - `SendARP`), `socket`, `smtplib`, `ssl`
* **Concorrência**: `ThreadPoolExecutor` (varredura paralela ultrarrápida) e `threading`
* **Interface da Bandeja**: `pystray` + `Pillow`
* **Frontend**: HTML5, Vanilla CSS3, JavaScript assíncrono (sem dependências pesadas de terceiros)
* **Empacotamento**: PyInstaller

---

## 📦 Como Executar

### 1. Via Executável (.exe)
Basta baixar ou acessar a pasta `dist/` e executar:
```text
dist/MonitorNOC.exe
```
O painel abrirá automaticamente no seu navegador padrão em: **`http://127.0.0.1:5000`** e o ícone ficará ativo na bandeja do Windows.

### 2. A Partir do Código Fonte (Python)
Clone o repositório e instale as dependências:
```bash
git clone https://github.com/Davisvasconcellos/noc-monitor.git
cd noc-monitor

pip install flask pystray pillow
python app.py
```

### 3. Gerando uma Nova Build (.exe)
Para compilar um novo executável autônomo de arquivo único (`--onefile`):
```cmd
build.bat
```
O executável final será gerado em `dist/MonitorNOC.exe`.

---

## 📄 Licença

**Monitor NOC** &copy; 2026 **Davis Vasconcellos**. Todos os direitos reservados.  
Licença: Livre para Uso e Distribuição • Não Comercial • Crédito Obrigatório.
