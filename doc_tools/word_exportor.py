from datetime import datetime
import os
import re
from typing import Dict

from doc_tools.word_generator import CustomWordGenerator


class WordExporter:
    """Word导出器"""
    
    @staticmethod
    def export_report(report_data: Dict, 
                     output_dir: str = "reports") -> str:
        """
        导出报告到Word
        
        Args:
            report_data: 报告数据
            output_dir: 输出目录
            config_path: 样式配置文件路径
            
        Returns:
            生成的Word文件路径
        """
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        
        # 创建Word生成器
        generator = CustomWordGenerator()
        generator.create_document()
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_title = report_data.get("report_title", "园区运营报告")
        meta_data = report_data.get("metadata")
        safe_title = re.sub(r'[<>:"/\\|?*]', '', report_title)[:50]
        filename = f"{safe_title}_{timestamp}.docx"
        filepath = os.path.join(output_dir, filename)
        
        
        #元数据处理
        data_range = meta_data.get("data_range", {})
        start_date = data_range.get("start_date", "N/A")
        end_date = data_range.get("end_date", "N/A")
        metadata={
            "报告周期：":f"{start_date} - {end_date}",
            "报告版本：":"V1.0",
            "编制部门：":"园区数字化运营中心",
            "生成时间：":meta_data.get("generated_time"),
            "分析维度：":meta_data.get("dimensions"),
            "数据源":meta_data.get("data_sources"),
        }
        
        try:
           
           # 添加封面页
            generator.add_cover_page(title=report_title,metadata=metadata)
             # 添加目录
            # generator.add_toc()
            # 添加报告内容
            content = report_data.get("report_content", "")
            if content:
                generator.add_markdown_content(content)
            
            # 添加附录
            # generator.add_heading("附录", level=1)
            # generator.add_cover_page()
           
            # 保存文档
            generator.save(filepath, auto_update_toc=True)
        
            return filepath
            
        except Exception as e:
            print(f"❌ 导出Word文档失败: {e}")
            # 尝试生成简化版本
            try:
                simple_path = os.path.join(output_dir, f"{filename}")
                generator.doc.save(simple_path)
                return simple_path
            except:
                return ""