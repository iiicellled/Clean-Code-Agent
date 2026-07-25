import re



def quicksort(customers: list) -> list:
    # 处理输入为 None 或空列表的边界情况
    if not customers:
        return []
        
    # 递归基：当列表长度小于等于 1 时，无需排序直接返回
    if len(customers) <= 1:
        return customers
        
    # 选取中间位置的元素作为基准值
    pivot = customers[len(customers) // 2]
    
    # 依据 id 属性将元素划分为小于、等于和大于基准值的三个子列表
    left = [x for x in customers if x.id < pivot.id]
    middle = [x for x in customers if x.id == pivot.id]
    right = [x for x in customers if x.id > pivot.id]
    
    # 递归排序左右子列表并拼接结果
    return quicksort(left) + middle + quicksort(right)

class Customer:
	def __init__(self, c_id, name, age='None', phone='None', email='None'):
		self.id = c_id
		self.name = name
		self.age = age
		self.phone = phone
		self.email = email

	@staticmethod
	def check_id(c_id):
		return c_id.isdigit()

	@staticmethod
	def check_name(name):
		return name.isalpha()

	@staticmethod
	def check_age(age):
		return age.isdigit()

	@staticmethod
	def check_phone(phone):
		return re.match(r'^1[345789]\d{9}$', phone)

	@staticmethod
	def check_email(email):
		pattern = r"[\w!#$%&'*+-/=?^`{|}~.]+@[\w!#$%&'*+-/=?^`{|}~.]+\.[a-zA-Z]{2,}$"
		return True if re.match(pattern, email) else False

	def __str__(self):
		return (f'Id: {self.id:<5}, Name: {self.name:<10}, Age: {self.age:<5}, Phone: {self.phone:<15}, Email: {self.email:<25}')

print(check_customer(Customer('1', 'pwx', '18','15606122516', 'pwx@gmail.com')))





