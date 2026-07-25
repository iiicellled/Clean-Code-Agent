import re
import time
from customer import Customer

class CMS:

	def __init__(self):
		self.customer_id_dict = {}
		self.customer_name_dict = {}


	def display_menu(self):
		print("""
			----------客户管理系统---------
					  1. 添加客户
					  2. 删除客户
					  3. 修改客户
					  4. 查询客户
					  5. 显示客户
					  6. 退出
			""")


	def add_customer_id(self):
		customer_id = 'None'
		for i in range(3):
			if i < 2:
				customer_id = input("请输入客户id: ")
				if Customer.check_id(customer_id):
					break
				else:
					print('客户id必须为纯数字')
			else:
				customer_id = input("最后一次机会, 请输入客户id: ")
				if Customer.check_id(customer_id):
					break
				else:
					print("终止添加客户")
					return False
		if customer_id in self.customer_id_dict:
			print('客户id已存在, 终止添加客户')
			return False
		else:
			return customer_id


	def add_customer_name(self):
		customer_name = 'None'
		for i in range(3):
			if i < 2:
				customer_name = input("请输入客户姓名: ")
				if Customer.check_name(customer_name):
					break
				else:
					print('客户姓名必须为字符')
			else:
				customer_name = input("最后一次机会, 请输入客户姓名: ")
				if Customer.check_name(customer_name):
					break
				else:
					print("终止添加客户")
					return False
		return customer_name


	def set_customer_age(self):
		customer_age = input("请输入客户年龄: ")
		if Customer.check_age(customer_age):
			return customer_age
		else:
			print("年龄不合法, 暂时不添加也可以")
		return "None"


	def set_customer_phone(self):
		customer_phone = input("请输入客户电话: ")
		if Customer.check_phone(customer_phone):
			return customer_phone
		elif re.search(r"^[\d-]+$", customer_phone):
			print("这个电话不太常见, 但也可以添加")
			return customer_phone
		else:
			print("电话不合法, 暂时不添加也可以")
		return "None"


	def set_customer_email(self):
		customer_email = input("请输入客户邮箱: ")
		if Customer.check_email(customer_email):
			print("邮箱合法")
			return customer_email
		else:
			print("邮箱不合法, 暂时不添加也可以")
		return "None"


	def add_customer(self):
		if not (customer_id := self.add_customer_id()):
			return
		if not (customer_name := self.add_customer_name()):
			return

		customer_age = self.set_customer_age()
		customer_phone = self.set_customer_phone()
		customer_email = self.set_customer_email()

		customer = Customer(customer_id, customer_name, customer_age, customer_phone, 
							customer_email)
		self.customer_id_dict[customer_id] = customer

		customer_inner_dict = self.customer_name_dict.get(customer_name)
		if customer_inner_dict is None:
			self.customer_name_dict[customer_name] = {customer_id: customer}
		else:
			customer_inner_dict[customer_id] = customer

		print(f'添加客户{customer_id}成功')


	def delete_customer(self):
		customer_id = input("请输入要删除的客户id: ")
		if not Customer.check_id(customer_id):
			print("客户id必须为纯数字")
			print("终止删除客户")
			return
		if customer_id not in self.customer_id_dict:
			print("客户id不存在")
			print("终止删除客户")
			return
		else:
			customer_name = self.customer_id_dict[customer_id].name

		if input("是否确定删除? [y/n] ").lower() == "y":
			del self.customer_id_dict[customer_id]

			customer_inner_dict = self.customer_name_dict.get(customer_name)
			del customer_inner_dict[customer_id]
			if len(customer_inner_dict) == 0:
				del self.customer_name_dict[customer_name]

			print(f'客户{customer_id}删除完毕')
		else:
			print("终止删除客户")
			return


	def update_customer(self):
		customer_id = input("请输入要修改的客户id: ")
		if not Customer.check_id(customer_id):
			print("客户id必须为纯数字")
			print("终止修改客户")
			return
		if customer_id not in self.customer_id_dict:
			print("客户id不存在")
			print("终止修改客户")
			return
		else:
			customer_name = self.customer_id_dict[customer_id]

		print(f'客户{customer_id}的历史年龄: ', self.customer_id_dict[customer_id].age)
		if (customer_age := self.set_customer_age()) != "None":
			self.customer_id_dict[customer_id].age = customer_age

		print(f'客户{customer_id}的历史电话: ', self.customer_id_dict[customer_id].phone)
		if (customer_phone := self.set_customer_phone()) != "None":
			self.customer_id_dict[customer_id].phone = customer_phone

		print(f'客户{customer_id}的历史邮箱: ', self.customer_id_dict[customer_id].email)
		if (customer_email := self.set_customer_email()) != "None":
			self.customer_id_dict[customer_id].email = customer_email

		print(f'客户{customer_id}修改完毕')


	def search_customer(self):
		customer_info = input("请输入要查询的客户id或姓名: ")
		if Customer.check_id(customer_info):
			if customer_info in self.customer_id_dict:
				print(self.customer_id_dict[customer_info])
			else:
				print('客户id不存在')
		elif Customer.check_name(customer_info):
			if customer_info in self.customer_name_dict:
				for customer_id in self.customer_name_dict[customer_info]:
					print(self.customer_name_dict[customer_info][customer_id])
			else:
				print("客户姓名不存在")
		else:
			print("输入的好像不是客户id或姓名")


	def display_customer(self):
		if len(self.customer_id_dict) == 0:
			print("暂无客户信息")
		for i in self.customer_id_dict:
			print(self.customer_id_dict[i])


	def start(self):
		try:
			while True:
				self.display_menu()
				choice = input("<< ")
				match choice:
					case "1":
						self.add_customer()
					case "2":
						self.delete_customer()
					case "3":
						self.update_customer()
					case "4":
						self.search_customer()
					case "5":
						self.display_customer()
					case "6":
						print(f"{"\b \b"*100}退出客户管理系统")
						break
					case _:
						print(">> ???")
						time.sleep(1)

		except (EOFError, KeyboardInterrupt):
			print(f"{"\b \b"*100}退出客户管理系统")


if __name__ == "__main__":
	cms = CMS()
	cms.start()


