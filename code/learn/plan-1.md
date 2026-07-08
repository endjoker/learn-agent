#LLM 基础与 API 实践
    ##计划：
    1、学习 Prompt 设计 | System Prompt、Few-shot、Chain-of-Thought
    2、流式响应原理 | SSE（Server-Sent Events）机制 | 修改 `agent.py` 观察流式输出
    3、配置管理 | 环境变量、API Key 安全 | 完善 `.env` 配置文件 |
    4、阅读 HelloAgents 框架的 `core/llm.py` | 理解框架的 LLM 抽象层 | 对照源码看自己代码的差距 |

    ##计划内容详情：
    1-->搁置在学习到system和prompt设计时在开始
    2-->1）先写一个搜索工具（web搜索，本地搜索，记忆搜索（放到记忆检索学习章节中设计，向量搜索）），先根据本地搜索方式创建tool调用接口：
            - Read：读取文件
            - Write：创建或覆盖文件
            - Edit：对文件做精确修改
            - Grep：搜索内容
            - Glob：查找文件
            - Bash：执行 Shell 命令
        


    ##计划进度：
    1、进度搁置
    2、2-1：已完成本地搜索代码编写-->完成代码调试-->完成react流式响应原理学习-->完成了本地大模型兼容配置 -->完成组合多个工具完成复杂任务，增加调用链机制-->完成异步工具的支持-->完成6个基本工具的增加
 
    ##知识点：
    1、count(x) 是一个非常实用的内置方法，它的核心功能就是计数，统计X在列表中完全匹配的出现次数

    2、split('@', maxsplit=1)：它能把一串长长的字符串，按照你指定的@切成多段，然后装进一个列表（List）里，maxsplit=1代表切一次

    3、tokens[1:] 表示“除了第一个元素，剩下的所有元素”。相当于把队伍的第一个人拎走，后面的人整体向前挪了一位。
        tokens = ['我', '爱', 'datawhale', '编程']
        print(tokens[1:])  
        # 输出：['爱', 'datawhale', '编程']

    4、zip() 就像一条拉链，它会把多个列表中同一位置的元素“咬合”在一起，形成一个一个的元组
        tokens = ['我', '爱', 'datawhale', '编程']
        # 执行错位拉链
        bigrams = zip(tokens, tokens[1:])
        # zip() 返回的是一个迭代器，用 list() 包起来才能看到里面的内容
        print(list(bigrams))  //注：生成元祖后必须使用list输出，内存地址
        # 输出：[('我', '爱'), ('爱', 'datawhale'), ('datawhale', '编程')]
        注：返回的是迭代器（一次性的），只能变量调用一次，需要长时间保存需要使用list(zip(tokens, tokens[1:]))

    5、Counter() 是 Python 标准库 collections 模块里的一个超级计数神器。它的核心功能就是：接收一串数据（可迭代对象），自动统计每个元素出现了多少次，并把结果以“字典（dict）”的形式返回给你
        fruits = ['apple', 'banana', 'apple', 'orange', 'banana', 'apple']
        counter = collections.Counter(fruits)
        print(counter)
        # 输出：Counter({'apple': 3, 'banana': 2, 'orange': 1})
        # 你可以像操作字典一样取出某个具体的值：
        print(counter['apple'])  # 输出：3
        print(counter['grape'])  # 输出：0 （不存在的返回0，不会报错）
    Counter 最精华的功能（必学）：.most_common(n):
        top_2 = bigram_counts.most_common(2)
        print(top_2)
        # 输出：[(('datawhale', 'is'), 2), (('is', 'great'), 1)]
        # 注意：返回的是列表，里面嵌套着 (元素, 次数) 的元组

6、numpy.array:是 NumPy（Numerical Python）库中用来创建数组（Array）的核心语法
        import numpy as np
        # 普通 Python 列表
        my_list = [0.9, 0.8]
        print(my_list * 2)  
        # 输出：[0.9, 0.8, 0.9, 0.8] （不是数学乘法，而是把列表复制了一遍！）
        # NumPy 数组
        my_array = np.array([0.9, 0.8])
        print(my_array * 2)  
        # 输出：[1.8 1.6] （完美！把每个元素都乘以了2，这才是我们要的数学运算）
    拓展：.mean()平均值、.sum()和、.max()最大值、np.dot 是 NumPy 中最核心、最常用的线性代数运算函数，它的全称是 Dot Product（点积）,Python 3.5 开始，@ 运算符被定义为矩阵乘法操作符，效果和 np.dot 完全一样.
        scores = np.array([0.9, 0.8])
        scores = np.array([1.0, 0.5])
        # 你想算平均情绪分
        average_score = scores.mean()  
        print(average_score)  # 输出：0.85
        # 你想找出最高分
        max_score = scores.max()   
        print(max_score)       # 输出：0.9
        result = np.dot(weights, features)
        # 手算过程：0.9*1.0 + 0.8*0.5 = 0.9 + 0.4 = 1.3
        print(result)  
        # 输出：1.3
        result = a @ b  # 完全等价于 np.dot(a, b)(python3.5以上)
        print(result)   # 输出：1.3

7、np.linalg.norm() 是 NumPy 线性代数模块（linalg）中的“求范数”函数。通俗来讲，它的核心作用就是：计算向量（或矩阵）的“长度”或“大小”。
        import numpy as np
        v = np.array([3, 4])
        # 方法1：直接用 norm
        norm_v = np.linalg.norm(v)  
        print(norm_v)  # 输出：5.0  （sqrt(3^2+4^2) = 5）
        # 方法2：用之前学的 dot 自己点自己，再开方
        dot_v = np.sqrt(np.dot(v, v))  
        print(dot_v)   # 输出：5.0
        # 验证：np.dot(v, v) = 3*3 + 4*4 = 25, sqrt(25)=5 //sqrt()：开更好