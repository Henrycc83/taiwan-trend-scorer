@echo off
chcp 65001 >nul
title 台股趨勢評分器
py -3 "%~dp0server.py"
