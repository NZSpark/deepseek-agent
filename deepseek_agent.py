import asyncio
import re
from pathlib import Path
from playwright.async_api import async_playwright

class DeepSeekWebAgent:
    def __init__(self, user_data_dir: str = "./user_data"):
        """
        :param user_data_dir: 存放浏览器 Cookie / Session 的本地目录，避免重复登录
        """
        self.user_data_dir = user_data_dir
        self.playwright = None
        self.context = None
        self.page = None

    async def start(self):
        """启动浏览器并打开 DeepSeek Chat"""
        self.playwright = await async_playwright().start()
        # 使用 persistent_context 可以保存登录状态（Cookie/LocalStorage）
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=False,  # 第一次运行时建议设置为 False，手动完成登录/验证码
            args=["--disable-blink-features=AutomationControlled"]
        )
        self.page = await self.context.new_page()
        print("正在打开 DeepSeek Chat...")
        await self.page.goto("https://chat.deepseek.com/")
        
        # 等待页面加载，提示用户手动完成登录（如需）
        print("提示：如果页面未登录，请在弹出的浏览器中手动登录DeepSeek，登录后按下回车继续...")
        input("按 Enter 键继续...")

    async def send_message(self, message: str) -> str:
        """发送消息并等待 AI 回复完成"""
        # 定位输入框 (DeepSeek 的文本输入框选择器)
        chat_input = await self.page.wait_for_selector('textarea[placeholder*="发送"]', timeout=10000)
        await chat_input.fill(message)
        
        # 点击发送按钮或按下 Enter
        await self.page.keyboard.press("Enter")
        print("消息已发送，等待 DeepSeek 响应...")

        # 等待 DeepSeek 回复结束（等待“停止生成”按钮消失或生成按钮重新出现）
        # 这里通过轮询最新回复内容直到稳定
        await asyncio.sleep(2)
        
        last_text = ""
        while True:
            # 获取页面中所有 AI 回复的 Block
            responses = await self.page.query_selector_all('.ds-markdown')
            if responses:
                latest_response = responses[-1]
                current_text = await latest_response.inner_text()
                
                # 如果文本内容不再变化，说明生成已完成
                if current_text == last_text and len(current_text) > 0:
                    break
                last_text = current_text
            await asyncio.sleep(1.5)

        print("DeepSeek 回复完成！")
        return last_text

    def extract_and_save_code(self, response_text: str, output_dir: str = "./output") -> list:
        """
        从回复内容中解析 markdown 代码块，并自动存为对应的文件格式
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved_files = []

        # 使用正则提取 Markdown 代码块 ```language ... ```
        pattern = r"```(\w+)?\n(.*?)```"
        matches = re.findall(pattern, response_text, re.DOTALL)

        # 常用语言到扩展名的映射表
        ext_map = {
            "python": "py",
            "py": "py",
            "javascript": "js",
            "js": "js",
            "html": "html",
            "css": "css",
            "json": "json",
            "cpp": "cpp",
            "c": "c",
            "bash": "sh",
            "shell": "sh",
            "sql": "sql",
            "markdown": "md",
            "md": "md"
        }

        for idx, (lang, code) in enumerate(matches, start=1):
            lang = lang.lower().strip() if lang else "txt"
            ext = ext_map.get(lang, "txt")
            
            filename = f"extracted_code_{idx}.{ext}"
            file_path = Path(output_dir) / filename
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code.strip())
            
            saved_files.append(str(file_path))
            print(f"已保存代码文件: {file_path}")

        # 如果没有提取到代码块，则将完整对话保存为 Markdown 文本
        if not matches:
            md_path = Path(output_dir) / "conversation_response.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(response_text)
            saved_files.append(str(md_path))
            print(f"未检测到明确代码块，已将完整回复存为 Markdown: {md_path}")

        return saved_files

    async def close(self):
        """关闭浏览器进程"""
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()


# ==================== 使用示例 ====================
async def main():
    agent = DeepSeekWebAgent()
    await agent.start()

    try:
        # 第一轮对话：请求编写 Python 脚本
        prompt1 = "请写一个Python脚本，功能是抓取指定网页的标题并保存为JSON文件。直接给出完整的代码。"
        reply1 = await agent.send_message(prompt1)
        
        # 提取会话中的代码块并存为文件（如 .py）
        agent.extract_and_save_code(reply1)

        # 第二轮对话：维持会话上下文
        prompt2 = "给刚才的代码加上异常处理逻辑，并在控制台输出详细日志。"
        reply2 = await agent.send_message(prompt2)
        
        # 再次提取优化后的代码
        agent.extract_and_save_code(reply2)

    finally:
        await agent.close()

if __name__ == "__main__":
    asyncio.run(main())