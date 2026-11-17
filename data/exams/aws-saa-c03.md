# AWS Solutions Architect – Associate (SAA-C03)

> Đề tiếng Anh theo exam guide SAA-C03: tình huống + 4 đáp án, hoặc chọn 2 trong 5 (Select TWO).

Nguồn (kiểm ngày 17/11/2026): exam guide SAA-C03 trên docs.aws.amazon.com/aws-certification, trang chứng chỉ aws.amazon.com/certification/certified-solutions-architect-associate, bộ 10 câu mẫu chính thức của AWS (Sample Exam Questions PDF).

## Đề thật

- 65 câu trong 130 phút: 50 câu tính điểm, 15 câu thử nghiệm không tính điểm và không được báo trước. Thang điểm 100 đến 1000, đậu từ 720. Bỏ trống tính là sai, đoán sai không bị trừ.
- Bốn domain: Design Secure Architectures 30%, Design Resilient Architectures 26%, Design High-Performing Architectures 24%, Design Cost-Optimized Architectures 20%.
- Chỉ có hai dạng câu. Multiple choice: một đáp án đúng, ba đáp án nhiễu. Multiple response: từ hai đáp án đúng trở lên trong năm lựa chọn trở lên, đề ghi "(Select TWO.)" hoặc "(Select THREE.)". Các dạng ordering, matching, case study AWS đưa vào từ 2024 chỉ có ở một số đề khác như AIF-C01, MLA-C01; không ra cho lớp này.
- Exam guide nói đáp án nhiễu là "plausible responses that match the content area", tức lựa chọn mà người nắm chưa chắc sẽ chọn.

## Cách viết đề (toàn bộ đề bằng tiếng Anh, đúng giọng văn AWS)

- Tình huống 2 đến 4 câu, ngôi thứ ba: "A company runs…", "A solutions architect needs to design…", "An ecommerce company is planning…". Nêu hiện trạng, vấn đề, rồi ràng buộc. Lần đầu nhắc tên dịch vụ thì viết đầy đủ: "Amazon Simple Queue Service (Amazon SQS)", "Amazon EC2 instances", "an Amazon RDS for MySQL DB instance".
- Ràng buộc là chỗ đặt keyword. Mỗi câu có một hai ràng buộc quyết định đáp án, lẫn trong vài chi tiết không quan trọng. Ví dụ: "without downtime", "cannot be interrupted before completion", "cannot be directly accessible from the internet", "encryption keys stored on premises", "must be present when the instances resume", "near real time", "fully managed", "minimal changes to the application code", "unpredictable traffic", "sub-millisecond latency", "in a single Availability Zone", "for compliance reasons, must be retained for 7 years".
- Câu hỏi đứng riêng một đoạn, dùng đúng các mẫu của AWS:
  - "Which solution will meet these requirements?"
  - "Which solution will meet these requirements MOST cost-effectively?"
  - "Which solution will meet these requirements with the LEAST operational overhead?"
  - "Which combination of steps should a solutions architect take to …? (Select TWO.)"
  - "What should a solutions architect do to …?" / "Which approach should the solutions architect recommend …?"
  Từ nhấn MOST, LEAST viết hoa như đề thật. Từ nhấn này cũng là keyword: hai đáp án cùng chạy được thì nó quyết định chọn cái nào.
- Đáp án ghi "A)", "B)"…, mỗi đáp án là một hành động cụ thể, bắt đầu bằng động từ (Configure, Create, Deploy, Use, Migrate, Enable, Attach). Các đáp án dài ngắn và giọng văn tương đương nhau, đừng để đáp án đúng dài hẳn ra.
- Đáp án nhiễu kiểu AWS: đúng lĩnh vực nhưng sai một chi tiết (Spot Instances cho job không được ngắt; đọc từ Multi-AZ standby; NAT instance đặt trong private subnet; route private subnet thẳng ra internet gateway); hoặc chạy được nhưng tốn công, tốn tiền hơn khi đề hỏi LEAST operational overhead hay MOST cost-effectively; hoặc dịch vụ có thật nhưng giải quyết vấn đề khác. Không dùng "All of the above", "None of the above".
- Khoảng một phần tư số câu là Select TWO (năm đáp án). Ra bộ nhiều câu thì rải theo tỉ lệ bốn domain, ưu tiên phần mình đang học.
- Chỉ dùng dịch vụ và tính năng còn nằm trong phạm vi SAA-C03.

## Keyword khi chấm

Keyword đúng là ràng buộc trong tình huống cộng với từ nhấn trong câu hỏi. Mình nêu được ràng buộc loại đáp án nhiễu (ví dụ "cannot be interrupted" loại Spot) là trúng. Chỉ nêu tên dịch vụ hay ý chung chung ("vì nó tốt hơn", "vì high availability" khi đề không nói tới) là sai.

## Một câu mẫu chính thức (chỉ để học giọng văn, đừng hỏi lại nguyên câu)

> A company uses Amazon EC2 Reserved Instances to run its data processing workload. The nightly job typically takes 7 hours to run and must finish within a 10-hour time window. The company anticipates temporary increases in demand at the end of each month that will cause the job to run over the time limit with the capacity of the current resources. Once started, the processing job cannot be interrupted before completion. The company wants to implement a solution that would provide increased resource capacity as cost-effectively as possible.
>
> What should a solutions architect do to accomplish this?
>
> A) Deploy On-Demand Instances during periods of high demand.
> B) Create a second EC2 reservation for additional instances.
> C) Deploy Spot Instances during periods of high demand.
> D) Increase the EC2 instance size in the EC2 reservation to support the increased workload.

Đáp án A. Keyword: "cannot be interrupted" loại C dù Spot rẻ nhất; "temporary increases … at the end of each month" loại B và D vì đặt thêm reservation thì trả tiền cả tháng cho nhu cầu vài ngày.
