本程序是运行telegram机器人的主程序，全靠chatgpt、claude.ai写出来的
除了主程序外，还需要你自己在主机上申请好证书，做好nginx反代({website}反代到15000端口，要开启证书）
运行成功后，还需要把webhook弄好，基本上就能作为一个基础版本的客服机器人了
webhook格式：
curl -s -X POST "https://api.telegram.org/bot{your_bot_token}/setWebhook" -d "url=https://{website}/{your_bot_token}//webhook"
