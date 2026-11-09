@echo off
rem 开放家语字幕屏局域网访问：仅“专用网络”入站 TCP 7020
rem 用法：右键本文件 → 以管理员身份运行
netsh advfirewall firewall add rule name=familyasr-lan dir=in action=allow protocol=TCP localport=7020 profile=private
echo.
echo 完成。若上方显示“确定。”，则规则已生效，可以关掉本窗口。
echo （删除规则：netsh advfirewall firewall delete rule name=familyasr-lan）
pause
